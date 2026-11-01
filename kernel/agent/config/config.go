// Package config 提供 console-agent 的配置加载：
// 默认值 → 配置文件 → 环境变量 → 命令行 flag，后加载者覆盖先加载者。
//
// 配置文件默认路径 /etc/console-agent/config.yaml，内容为 JSON
// （JSON 是 YAML 1.2 的子集，故沿用 .yaml 扩展名，标准库可直接解析）。
package config

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

// Version 是 agent 自身版本；ProtocolVersion 是上报协议版本（与接收端共同演进）。
const (
	Version         = "0.1.0"
	ProtocolVersion = "v1"
)

// IngestPath 是接收端上报路径（B4-3 契约）。
const IngestPath = "/api/monitoring/v1/ingest"

// DefaultConfigPath 是配置文件默认路径。
const DefaultConfigPath = "/etc/console-agent/config.yaml"

// DefaultDockerSocket 是 Docker Engine API unix socket 默认路径。
const DefaultDockerSocket = "/var/run/docker.sock"

// Defaults 收集全部默认值，供 Load 使用。
type Defaults struct {
	ServerURL    string
	MachineID    string
	EnrollToken  string
	StateDir     string
	DockerSocket string
	SampleSecond int
	BufferByte   int64
	DiskPath     string
}

// Default 返回内置默认值。
func Default() Defaults {
	return Defaults{
		ServerURL:    "http://127.0.0.1:8081",
		MachineID:    "",
		EnrollToken:  "",
		StateDir:     "/var/lib/console-agent",
		DockerSocket: DefaultDockerSocket,
		SampleSecond: 300,
		BufferByte:   100 << 20,
		DiskPath:     "/",
	}
}

// Config 是运行配置（上报、缓冲、采集频率）。
// 身份密钥与运行配置分离：密钥由 enrol 流程写入 state 目录，不进 Config。
type Config struct {
	// ServerURL 是 Console 接收端基址，如 http://100.64.0.10:8081
	ServerURL string `json:"server_url"`
	// MachineID 是台账登记的机器 id（nas/txy/aly/kr/sg/us），enrol 时绑定。
	MachineID string `json:"machine_id"`
	// StateDir 存放 identity.json 与 buffer JSONL；磁盘开销以此目录为限。
	StateDir string `json:"state_dir"`
	// DockerSocket 是 Docker Engine API 的 unix socket 路径。
	DockerSocket string `json:"docker_socket"`
	// DiskPath 是磁盘使用率采集的挂载点（B6-1 裁决：默认 /，多盘机器如 NAS
	// 显式配置主数据盘 /vol1 以对齐 Beszel「主数据盘」口径）。
	DiskPath string `json:"disk_path"`

	sampleSecond int
	bufferByte   int64
	enrollToken  string
}

// EnrollToken 返回首次注册令牌（仅 enrol 阶段使用）。
func (c *Config) EnrollToken() string { return c.enrollToken }

// Validate 校验配置完整、可运行。
func (c *Config) Validate() error {
	if c.ServerURL == "" {
		return errors.New("server_url 不能为空")
	}
	if !strings.HasPrefix(c.ServerURL, "http://") && !strings.HasPrefix(c.ServerURL, "https://") {
		return fmt.Errorf("server_url 必须以 http:// 或 https:// 开头: %q", c.ServerURL)
	}
	c.ServerURL = strings.TrimSuffix(c.ServerURL, "/")
	if c.MachineID == "" {
		return errors.New("machine_id 不能为空（需与台账登记一致）")
	}
	if strings.ContainsAny(c.MachineID, " /\\") {
		return fmt.Errorf("machine_id 含非法字符: %q", c.MachineID)
	}
	if c.StateDir == "" {
		return errors.New("state_dir 不能为空")
	}
	if c.sampleSecond <= 0 {
		return fmt.Errorf("采样间隔必须为正: %d", c.sampleSecond)
	}
	if c.bufferByte <= 0 {
		return fmt.Errorf("缓冲上限必须为正: %d", c.bufferByte)
	}
	if c.DockerSocket == "" {
		c.DockerSocket = DefaultDockerSocket
	}
	if c.DiskPath == "" {
		c.DiskPath = "/"
	}
	return nil
}

// SampleIntervalDur 返回采样间隔。
func (c *Config) SampleIntervalDur() time.Duration {
	return time.Duration(c.sampleSecond) * time.Second
}

// BufferMax 返回缓冲字节上限。
func (c *Config) BufferMax() int64 { return c.bufferByte }

// MarshalJSON 以磁盘可读形状序列化（内部字段转秒/字节）。
func (c *Config) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		ServerURL         string `json:"server_url"`
		MachineID         string `json:"machine_id"`
		StateDir          string `json:"state_dir"`
		DockerSocket      string `json:"docker_socket"`
		DiskPath          string `json:"disk_path"`
		SampleIntervalSec int    `json:"sample_interval_sec"`
		BufferMaxBytes    int64  `json:"buffer_max_bytes"`
	}{
		ServerURL:         c.ServerURL,
		MachineID:         c.MachineID,
		StateDir:          c.StateDir,
		DockerSocket:      c.DockerSocket,
		DiskPath:          c.DiskPath,
		SampleIntervalSec: c.sampleSecond,
		BufferMaxBytes:    c.bufferByte,
	})
}

// fileConfig 与磁盘 JSON 的形状对应（宽松：未知字段忽略，已知字段类型严格）。
type fileConfig struct {
	ServerURL    *string `json:"server_url"`
	MachineID    *string `json:"machine_id"`
	EnrollToken  *string `json:"enroll_token"`
	StateDir     *string `json:"state_dir"`
	DockerSocket *string `json:"docker_socket"`
	DiskPath     *string `json:"disk_path"`
	SampleSec    *int    `json:"sample_interval_sec"`
	BufferBytes  *int64  `json:"buffer_max_bytes"`
}

// Load 按默认值 → 配置文件 → 环境变量 → flag 的顺序合成配置。
// 配置文件不存在不报错（纯 flag/env 也能跑）；flag 显式值覆盖前两层。
func Load(fs *flag.FlagSet, args []string) (*Config, error) {
	d := Default()

	var (
		configPath = DefaultConfigPath
		serverURL  = d.ServerURL
		machineID  = d.MachineID
		enrollTok  = d.EnrollToken
		stateDir   = d.StateDir
		sampleSec  = d.SampleSecond
		bufferByte = d.BufferByte
		diskPath   = d.DiskPath
	)
	if fs == nil {
		fs = flag.NewFlagSet("console-agent", flag.ContinueOnError)
	}
	fs.StringVar(&configPath, "config", configPath, "配置文件路径")
	fs.StringVar(&serverURL, "server", serverURL, "Console 接收端基址")
	fs.StringVar(&machineID, "machine-id", machineID, "台账机器 id")
	fs.StringVar(&enrollTok, "enroll-token", enrollTok, "首次注册令牌（enrol 后不再需要）")
	fs.StringVar(&stateDir, "state-dir", stateDir, "状态目录（identity/buffer）")
	fs.StringVar(&diskPath, "disk-path", diskPath, "磁盘使用率采集挂载点（默认 /，多盘机器显式指定主数据盘如 /vol1）")
	fs.IntVar(&sampleSec, "sample-interval", sampleSec, "采样间隔秒（默认 300）")
	fs.Int64Var(&bufferByte, "buffer-bytes", bufferByte, "断网缓冲字节上限")
	if err := fs.Parse(args); err != nil {
		return nil, err
	}

	c := &Config{
		ServerURL:    d.ServerURL,
		MachineID:    d.MachineID,
		StateDir:     d.StateDir,
		DockerSocket: d.DockerSocket,
		DiskPath:     d.DiskPath,
		sampleSecond: d.SampleSecond,
		bufferByte:   d.BufferByte,
	}

	// 文件层
	fc, err := loadFile(configPath)
	if err != nil {
		return nil, err
	}
	applyFile(c, fc)

	// 环境变量层
	applyEnv(c)

	// flag 层：与内置默认值不同即视为显式设置，覆盖前两层。
	if serverURL != d.ServerURL {
		c.ServerURL = serverURL
	}
	if machineID != d.MachineID {
		c.MachineID = machineID
	}
	if stateDir != d.StateDir {
		c.StateDir = stateDir
	}
	if diskPath != d.DiskPath {
		c.DiskPath = diskPath
	}
	if sampleSec != d.SampleSecond {
		c.sampleSecond = sampleSec
	}
	if bufferByte != d.BufferByte {
		c.bufferByte = bufferByte
	}

	// 注册令牌：文件 → env → flag 同样取显式者。
	c.enrollToken = fc.token()
	if v := os.Getenv("CONSOLE_AGENT_ENROLL_TOKEN"); v != "" {
		c.enrollToken = v
	}
	if enrollTok != d.EnrollToken {
		c.enrollToken = enrollTok
	}

	if err := c.Validate(); err != nil {
		return nil, err
	}
	return c, nil
}

func (fc *fileConfig) token() string {
	if fc.EnrollToken != nil && *fc.EnrollToken != "" {
		return *fc.EnrollToken
	}
	return ""
}

func loadFile(path string) (*fileConfig, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return &fileConfig{}, nil
		}
		return nil, fmt.Errorf("读配置文件 %s: %w", path, err)
	}
	fc := &fileConfig{}
	if err := json.Unmarshal(data, fc); err != nil {
		return nil, fmt.Errorf("解析配置文件 %s: %w", path, err)
	}
	return fc, nil
}

// applyEnv 用环境变量覆盖已知字段；空值跳过。
func applyEnv(c *Config) {
	if v := os.Getenv("CONSOLE_AGENT_SERVER"); v != "" {
		c.ServerURL = v
	}
	if v := os.Getenv("CONSOLE_AGENT_MACHINE_ID"); v != "" {
		c.MachineID = v
	}
	if v := os.Getenv("CONSOLE_AGENT_STATE_DIR"); v != "" {
		c.StateDir = v
	}
	if v := os.Getenv("CONSOLE_AGENT_SAMPLE_INTERVAL"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			c.sampleSecond = n
		}
	}
	if v := os.Getenv("CONSOLE_AGENT_BUFFER_BYTES"); v != "" {
		if n, err := strconv.ParseInt(v, 10, 64); err == nil {
			c.bufferByte = n
		}
	}
	if v := os.Getenv("CONSOLE_AGENT_DOCKER_SOCKET"); v != "" {
		c.DockerSocket = v
	}
	if v := os.Getenv("CONSOLE_AGENT_DISK_PATH"); v != "" {
		c.DiskPath = v
	}
}

func applyFile(c *Config, fc *fileConfig) {
	if fc.ServerURL != nil {
		c.ServerURL = *fc.ServerURL
	}
	if fc.MachineID != nil {
		c.MachineID = *fc.MachineID
	}
	if fc.StateDir != nil {
		c.StateDir = *fc.StateDir
	}
	if fc.DockerSocket != nil {
		c.DockerSocket = *fc.DockerSocket
	}
	if fc.DiskPath != nil {
		c.DiskPath = *fc.DiskPath
	}
	if fc.SampleSec != nil {
		c.sampleSecond = *fc.SampleSec
	}
	if fc.BufferBytes != nil {
		c.bufferByte = *fc.BufferBytes
	}
}
