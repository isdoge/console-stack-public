// console-agent 是 console-stack 的薄 Go 采集端（B4-1）。
//
// 职责：周期采集主机与容器指标 → 断网时本地 JSONL 缓冲 → 恢复后按真实采样时间上报
// 到 Console 接收端（POST /api/monitoring/v1/ingest）。只读采集，不执行任意命令。
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"syscall"
	"time"

	"console-agent/collector"
	"console-agent/config"
	"console-agent/sender"
)

// runCommand 执行外部命令并返回合并输出（install/uninstall/upgrade 自检用）。
func runCommand(name string, args ...string) (string, error) {
	out, err := exec.Command(name, args...).CombinedOutput()
	return string(out), err
}

const serviceUnit = `/etc/systemd/system/console-agent.service`

func main() {
	log.SetFlags(log.LstdFlags | log.LUTC)
	log.SetPrefix("console-agent ")
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	var err error
	switch os.Args[1] {
	case "run":
		err = cmdRun(os.Args[2:])
	case "enrol":
		err = cmdEnrol(os.Args[2:])
	case "install":
		err = cmdInstall(os.Args[2:])
	case "uninstall":
		err = cmdUninstall(os.Args[2:])
	case "upgrade":
		err = cmdUpgrade(os.Args[2:])
	case "version":
		err = cmdVersion()
	default:
		usage()
		os.Exit(2)
	}
	if err != nil {
		log.Printf("FATAL %v", err)
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprintf(os.Stderr, `console-agent %s — console-stack 采集端

用法:
  console-agent run        采集主循环（前台运行，交给 systemd 托管）
  console-agent enrol      首次注册（--enroll-token 必填）
  console-agent install    安装 systemd 服务并启动
  console-agent uninstall  停止并移除 systemd 服务
  console-agent upgrade    下载升级包（--url + --sha256），校验失败拒绝安装
  console-agent version    打印版本

配置: 默认值 → %s → 环境变量 → 命令行 flag（后者覆盖前者）
  环境变量: CONSOLE_AGENT_SERVER / _MACHINE_ID / _STATE_DIR /
            _SAMPLE_INTERVAL / _BUFFER_BYTES / _ENROLL_TOKEN / _DOCKER_SOCKET
`, config.Version, config.DefaultConfigPath)
}

func cmdVersion() error {
	fmt.Printf("console-agent %s (protocol %s, %s/%s)\n", config.Version, config.ProtocolVersion, runtime.GOOS, runtime.GOARCH)
	return nil
}

func loadConfig(args []string) (*config.Config, error) {
	return config.Load(flag.NewFlagSet("run", flag.ContinueOnError), args)
}

func cmdRun(args []string) error {
	cfg, err := loadConfig(args)
	if err != nil {
		return err
	}
	id, err := sender.LoadIdentity(cfg.StateDir)
	if err != nil {
		return err
	}
	if id == nil {
		return errors.New("未注册：先运行 console-agent enrol --enroll-token <token>")
	}

	buf, err := sender.OpenBuffer(cfg.StateDir, cfg.BufferMax())
	if err != nil {
		return err
	}
	client := sender.NewClient(cfg.ServerURL, 30*time.Second)
	client.SetIdentity(id)
	docker := collector.NewDockerClient(cfg.DockerSocket, 10*time.Second)
	host := collector.NewHostCollector(minDur(cfg.SampleIntervalDur(), 60*time.Second), cfg.DiskPath)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	log.Printf("run 开始: machine=%s server=%s interval=%s buffer=%dMB 队列=%d",
		cfg.MachineID, cfg.ServerURL, cfg.SampleIntervalDur(), cfg.BufferMax()>>20, buf.Len())

	var seq uint64
	if id.EnrolledAt.After(time.Time{}) {
		seq = uint64(time.Now().UnixNano() & 0xffff) // 重启后的序列起点仍保持单调（见 README）
	}
	ticker := time.NewTicker(cfg.SampleIntervalDur())
	defer ticker.Stop()

	flush := func() {
		for {
			batch, err := buf.Take(64)
			if err != nil {
				log.Printf("缓冲读取失败: %v", err)
				return
			}
			if len(batch) == 0 {
				return
			}
			err = client.Send(ctx, &sender.Batch{
				Protocol:          config.ProtocolVersion,
				AgentVersion:      config.Version,
				MachineID:         cfg.MachineID,
				SampleIntervalSec: int(cfg.SampleIntervalDur().Seconds()),
				Samples:           batch,
			})
			if err != nil {
				log.Printf("上报失败（%d 样本留在缓冲）: %v", len(batch), err)
				return
			}
			if err := buf.Ack(); err != nil {
				log.Printf("确认缓冲失败: %v", err)
			}
			log.Printf("上报成功 %d 样本", len(batch))
		}
	}

	// 启动即先补传存量（断网恢复场景）。
	flush()

	for {
		sampleNow(ctx, cfg, host, docker, buf, &seq)
		flush()
		select {
		case <-ctx.Done():
			log.Printf("收到退出信号，结束")
			return nil
		case <-ticker.C:
		}
	}
}

// sampleNow 采一轮样本并压入缓冲；主机整组失败时不产生样本。
func sampleNow(ctx context.Context, cfg *config.Config, host *collector.HostCollector, docker *collector.DockerClient, buf *sender.Buffer, seq *uint64) {
	at := time.Now().UTC()
	p := &collector.Payload{
		Protocol:     config.ProtocolVersion,
		Agent:        config.Version,
		MachineID:    cfg.MachineID,
		SampledAt:    at,
		DroppedTotal: buf.Dropped(),
	}
	*seq++
	p.Seq = *seq

	hs, errs, err := host.Sample(ctx, at)
	if err != nil {
		log.Printf("主机采集失败（本轮无样本）: %v", err)
		return
	}
	p.Host = hs
	p.Errors = errs

	// 容器采集在主机 CPU 测量窗之后：容器帧自带 read/preread 时间元数据，
	// 接收端可核对每容器真实采样窗（cpu_delta_sec），不假装与主机同窗。
	cSamples, cErrs, err := collector.CollectContainers(ctx, docker)
	switch {
	case err != nil:
		// Docker socket 不可达 = 该机无容器能力或 daemon 停了；容器组缺席而非失败。
		if p.Errors == nil {
			p.Errors = map[string]string{}
		}
		p.Errors["docker"] = err.Error()
	case len(cSamples) > 0:
		p.Containers = cSamples
		for k, v := range cErrs {
			p.Errors["docker:"+k] = v
		}
	}

	if err := buf.Push(p); err != nil {
		log.Printf("缓冲写入失败: %v", err)
	}
}

func minDur(a, b time.Duration) time.Duration {
	if a < b {
		return a
	}
	return b
}

func cmdEnrol(args []string) error {
	cfg, err := loadConfig(args)
	if err != nil {
		return err
	}
	if cfg.EnrollToken() == "" {
		return errors.New("缺少 --enroll-token（或环境变量 CONSOLE_AGENT_ENROLL_TOKEN）")
	}
	client := sender.NewClient(cfg.ServerURL, 30*time.Second)
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	id, err := client.Enroll(ctx, cfg.MachineID, cfg.EnrollToken(), cfg.StateDir)
	if err != nil {
		return err
	}
	_ = id
	fmt.Printf("enrol 成功: machine=%s 身份文件=%s\n", cfg.MachineID, sender.IdentityPath(cfg.StateDir))
	return nil
}

func cmdInstall(args []string) error {
	cfg, err := loadConfig(args)
	if err != nil {
		return err
	}
	if _, err := sender.LoadIdentity(cfg.StateDir); err != nil {
		return err
	}
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	exe, err = filepath.EvalSymlinks(exe)
	if err != nil {
		return err
	}
	// 透传 --config：默认路径 /etc/console-agent/config.yaml 存在时仍需显式（service
	// 不带 --config 会读默认值；显式传保证 agent install --config 与 service 启动
	// 读同一文件，避免两处路径不一致）。
	configArg := ""
	for i, a := range args {
		if a == "-config" || a == "--config" {
			if i+1 < len(args) {
				configArg = " --config " + args[i+1]
			}
			break
		}
	}
	unit := fmt.Sprintf(`[Unit]
Description=console-stack monitoring agent
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=%s run%s
Restart=always
RestartSec=10
# 加固：无 shell、无新权限、只读访问大部分文件系统（docker.sock 只读挂载给采集）
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=%s
PrivateTmp=true

[Install]
WantedBy=multi-user.target
`, exe, configArg, cfg.StateDir)
	if err := os.WriteFile(serviceUnit, []byte(unit), 0o644); err != nil {
		return fmt.Errorf("写 service 单元: %w", err)
	}
	for _, c := range [][]string{{"systemctl", "daemon-reload"}, {"systemctl", "enable", "--now", "console-agent"}} {
		if out, err := runCommand(c[0], c[1:]...); err != nil {
			return fmt.Errorf("%v: %v\n%s", c, err, out)
		}
	}
	fmt.Printf("已安装并启动 console-agent.service（binary=%s state=%s）\n", exe, cfg.StateDir)
	return nil
}

func cmdUninstall(args []string) error {
	for _, c := range [][]string{{"systemctl", "disable", "--now", "console-agent"}} {
		if out, err := runCommand(c[0], c[1:]...); err != nil {
			fmt.Fprintf(os.Stderr, "%v: %v\n%s", c, err, out)
		}
	}
	if err := os.Remove(serviceUnit); err != nil && !os.IsNotExist(err) {
		return err
	}
	if out, err := runCommand("systemctl", "daemon-reload"); err != nil {
		return fmt.Errorf("daemon-reload: %v\n%s", err, out)
	}
	fmt.Println("已停止并移除 console-agent.service（state 目录与身份文件保留，手动删除请用 rm -rf " + config.Default().StateDir + ")")
	return nil
}

func cmdUpgrade(args []string) error {
	fs := flag.NewFlagSet("upgrade", flag.ExitOnError)
	url := fs.String("url", "", "升级包 URL")
	want := fs.String("sha256", "", "升级包 SHA256（hex 小写）")
	cfg, err := loadConfig(args)
	if err != nil {
		return err
	}
	_ = url
	_ = want
	// --url/--sha256 从 args 解析（loadConfig 忽略未知 flag 之外的参数）。
	for i := 0; i < len(args)-1; i++ {
		switch args[i] {
		case "--url", "-url":
			*url = args[i+1]
		case "--sha256", "-sha256":
			*want = args[i+1]
		}
	}
	if *url == "" || *want == "" {
		return errors.New("upgrade 需要 --url 与 --sha256")
	}
	up := &sender.Upgrade{Dir: cfg.StateDir + "/upgrade", Client: &http.Client{Timeout: 10 * time.Minute}}
	newBin, err := up.Download(*url, *want)
	if err != nil {
		return err
	}
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	return up.Apply(newBin, exe, func() error {
		out, err := runCommand(exe, "version")
		if err != nil {
			return fmt.Errorf("自检 version: %v\n%s", err, out)
		}
		return nil
	})
}
