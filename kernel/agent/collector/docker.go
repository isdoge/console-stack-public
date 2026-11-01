package collector

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"
)

// ComposeProjectLabel 是 Docker 官方的 compose 项目 label。
const ComposeProjectLabel = "com.docker.compose.project"

// ContainerInfo 来自 GET /containers/json 的行（列表阶段）。
type ContainerInfo struct {
	ID     string
	Name   string // 去掉前导 "/" 的 docker ps 名
	Image  string
	State  string // running/paused/exited（Docker 原文）
	Status string // "Up 2 hours (healthy)" 原文
	Labels map[string]string
}

// ContainerStats 来自 GET /containers/{id}/stats 的换算结果。
type ContainerStats struct {
	CPUPct            float64 // %，cpu/precpu 差分口径（100% = 全部算力，与 beszel-agent 同口径）
	CPUWindowSec      float64 // CPU 差分窗时长（read-preread 秒）；无有效窗时 0
	MemoryUsedBytes   uint64  // usage - inactive_file（beszel-agent 口径，inactive 缺席回退 cache）
	NetRecvBytesTotal uint64  // 全部网卡累计
	NetSentBytesTotal uint64
}

// minStatsWindowSec 是接受一个 CPU 差分窗的最短时长：窗太短（<100ms）时
// 除法放大抖动，视为无效基准。
const minStatsWindowSec = 0.1

// DockerClient 是 Docker Engine API 的只读客户端（unix socket，无第三方依赖）。
// 只调用 GET /containers/json 与 GET /containers/{id}/stats，不执行任何写操作。
type DockerClient struct {
	socket string
	client *http.Client
}

// NewDockerClient 创建 Docker API 客户端。timeout 是单请求超时。
func NewDockerClient(socketPath string, timeout time.Duration) *DockerClient {
	d := &DockerClient{socket: socketPath}
	d.client = &http.Client{
		Timeout: timeout,
		Transport: &http.Transport{
			DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
				var dialer net.Dialer
				return dialer.DialContext(ctx, "unix", socketPath)
			},
			// 每个请求独立连接：采样间隔长，连接复用徒增空闲句柄。
			DisableKeepAlives: true,
		},
	}
	return d
}

func (d *DockerClient) get(ctx context.Context, path string, out any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://docker"+path, nil)
	if err != nil {
		return err
	}
	resp, err := d.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("docker api %s: %s", path, resp.Status)
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

// ListContainers 返回运行中的容器列表（docker ps 口径）。
func (d *DockerClient) ListContainers(ctx context.Context) ([]ContainerInfo, error) {
	var rows []struct {
		ID     string            `json:"Id"`
		Names  []string          `json:"Names"`
		Image  string            `json:"Image"`
		State  string            `json:"State"`
		Status string            `json:"Status"`
		Labels map[string]string `json:"Labels"`
	}
	if err := d.get(ctx, "/containers/json", &rows); err != nil {
		return nil, err
	}
	infos := make([]ContainerInfo, 0, len(rows))
	for _, r := range rows {
		name := ""
		if len(r.Names) > 0 {
			name = strings.TrimPrefix(r.Names[0], "/")
		}
		infos = append(infos, ContainerInfo{
			ID: r.ID, Name: name, Image: r.Image, State: r.State, Status: r.Status, Labels: r.Labels,
		})
	}
	return infos, nil
}

// Stats 取单容器资源读数（受控双帧，见 statsJSON 文档）。
func (d *DockerClient) Stats(ctx context.Context, id string) (*ContainerStats, error) {
	var raw statsJSON
	if err := d.get(ctx, "/containers/"+id+"/stats?stream=false", &raw); err != nil {
		return nil, err
	}
	return parseStats(&raw)
}

// CollectContainers 采集全部运行中容器的资源读数。
// 列表失败返回 error（容器组整体缺席）；单容器 stats 失败记入 errs，不拖垮整组。
// 有界并发（4）+ 沿用 client 单请求超时：整轮耗时 ≈ ceil(n/4) × 单帧时长，
// 不因容器数增长而无界拖长采样窗。
func CollectContainers(ctx context.Context, client *DockerClient) ([]ContainerSample, map[string]string, error) {
	if client == nil {
		return nil, nil, nil
	}
	infos, err := client.ListContainers(ctx)
	if err != nil {
		return nil, nil, err
	}
	samples := make([]ContainerSample, 0, len(infos))
	errs := map[string]string{}
	var mu sync.Mutex
	var wg sync.WaitGroup
	// 小并发池：stats 单帧约需 1 秒，17 个容器串行会拉长采样窗。
	sem := make(chan struct{}, 4)
	for _, info := range infos {
		wg.Add(1)
		go func(info ContainerInfo) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()
			sample := ContainerSample{
				Name:           info.Name,
				ID:             info.ID,
				Image:          info.Image,
				State:          info.State,
				StatusText:     info.Status,
				Health:         HealthFromStatus(info.Status),
				ComposeProject: info.Labels[ComposeProjectLabel],
			}
			st, err := client.Stats(ctx, info.ID)
			if err != nil {
				mu.Lock()
				errs["docker:"+info.Name] = err.Error()
				mu.Unlock()
				// 资源读数缺席但容器行保留：容器列表是实查快照，不因单点失败丢行。
				sample.Health = "unknown"
			} else {
				sample.CPUPct = round1(st.CPUPct)
				sample.CPUDeltaSec = st.CPUWindowSec
				sample.MemoryUsedBytes = st.MemoryUsedBytes
				sample.NetRecvBytesTotal = st.NetRecvBytesTotal
				sample.NetSentBytesTotal = st.NetSentBytesTotal
				if st.CPUWindowSec <= 0 {
					// CPU 窗无效（daemon 无历史帧）：显式标记，报告侧据此豁免
					// CPU 比对而不是把 0 当真实测量。
					mu.Lock()
					errs["docker:"+info.Name] = "cpu window unavailable (no daemon history frame)"
					mu.Unlock()
				}
			}
			mu.Lock()
			samples = append(samples, sample)
			mu.Unlock()
		}(info)
	}
	wg.Wait()
	SortContainers(samples)
	return samples, errs, nil
}

// SortContainers 按名字稳定排序，保证样本顺序跨轮可比。
func SortContainers(s []ContainerSample) {
	for i := 1; i < len(s); i++ {
		for j := i; j > 0 && s[j].Name < s[j-1].Name; j-- {
			s[j], s[j-1] = s[j-1], s[j]
		}
	}
}

// HealthFromStatus 从 /containers/json 的 Status 原文推导 health 枚举。
// （"Up 2 hours (healthy)" / "Up 30 seconds (health: starting)" / 无括注 = none）
func HealthFromStatus(status string) string {
	switch {
	case strings.Contains(status, "unhealthy"):
		return "unhealthy"
	case strings.Contains(status, "healthy"):
		return "healthy"
	case strings.Contains(status, "health: starting"), strings.Contains(status, "starting"):
		return "starting"
	default:
		return "none"
	}
}

// statsJSON 与 Docker stats 响应对应的字段子集（只解析用到的）。
//
// CPU 口径（B6-1，与 beszel-agent CalculateCpuPercentLinux 对齐）：
//
//	stream=false 的单帧响应自带一对 daemon 亲测的样本——cpu_stats 与
//	precpu_stats，其间隔就是帧内 read 与 preread 的差（实测约 1s，随响应
//	带回、不依赖 daemon 历史）。因此不需要跨请求自存基准帧：直接用同帧
//	cpu/precpu 差分，窗口时长用 read/preread 校验（preread 必须是真实时刻
//	且早于 read，窗长 ≥ minStatsWindowSec），无效窗记 CPUWindowSec=0 并给
//	0%（缺失显式，不伪造测量）。
//
//	百分比不乘 online_cpus：cpuDelta/systemDelta 是「容器用掉的系统时间占比」，
//	×online_cpus 会把多核容器放大到 >100%（NAS 实测 hindsight-pg 105% 与
//	主机口径自相矛盾）；beszel 同为不乘的整机容量口径（100% = 全部算力）。
//
//	one-shot=1（Docker <25 有 precpu 全零 bug）不采用；stream=false 的
//	preread 始终是 daemon 的真实上一采样时刻。
type statsJSON struct {
	Read    time.Time `json:"read"`
	PreRead time.Time `json:"preread"`
	CPUStats    cpuStats `json:"cpu_stats"`
	PreCPUStats cpuStats `json:"precpu_stats"`
	MemoryStats struct {
		Usage uint64            `json:"usage"`
		Stats map[string]uint64 `json:"stats"`
	} `json:"memory_stats"`
	Networks map[string]struct {
		RxBytes uint64 `json:"rx_bytes"`
		TxBytes uint64 `json:"tx_bytes"`
	} `json:"networks"`
}

type cpuStats struct {
	CPUUsage struct {
		TotalUsage uint64 `json:"total_usage"`
	} `json:"cpu_usage"`
	SystemCPUUsage uint64 `json:"system_cpu_usage"`
	OnlineCPU      uint32 `json:"online_cpus"`
}

// parseStats 把 stats 响应换算为百分比/字节口径。纯函数，测试用 fixture 覆盖。
func parseStats(raw *statsJSON) (*ContainerStats, error) {
	st := &ContainerStats{}
	st.CPUPct, st.CPUWindowSec = cpuPercent(raw)
	st.MemoryUsedBytes = memUsed(raw.MemoryStats.Usage, raw.MemoryStats.Stats)
	for _, n := range raw.Networks {
		st.NetRecvBytesTotal += n.RxBytes
		st.NetSentBytesTotal += n.TxBytes
	}
	return st, nil
}

// cpuPercent 用同帧 cpu/precpu 差分算容器 CPU 百分比（整机容量口径）。
// 返回 (pct, windowSec)；窗无效时 pct=0、windowSec=0（调用方按缺失处理）。
func cpuPercent(raw *statsJSON) (float64, float64) {
	// preread 为零值 = daemon 没给历史帧（one-shot 或刚重启），基准不可信。
	if raw.PreRead.IsZero() || raw.Read.IsZero() || !raw.Read.After(raw.PreRead) {
		return 0, 0
	}
	window := raw.Read.Sub(raw.PreRead).Seconds()
	if window < minStatsWindowSec {
		return 0, 0
	}
	// 计数器回退（重置/乱序）：视作无基准，与 beszel 同为显式 0 而非负数。
	if raw.CPUStats.SystemCPUUsage <= raw.PreCPUStats.SystemCPUUsage ||
		raw.CPUStats.CPUUsage.TotalUsage < raw.PreCPUStats.CPUUsage.TotalUsage {
		return 0, window
	}
	cpuDelta := float64(raw.CPUStats.CPUUsage.TotalUsage - raw.PreCPUStats.CPUUsage.TotalUsage)
	sysDelta := float64(raw.CPUStats.SystemCPUUsage - raw.PreCPUStats.SystemCPUUsage)
	if sysDelta <= 0 {
		return 0, window
	}
	return cpuDelta / sysDelta * 100, window
}

// memUsed 换算 beszel-agent 口径的已用内存：usage - inactive_file，
// inactive_file 缺席或为 0 时回退 cache；减完为负按 0（不产生假值）。
func memUsed(usage uint64, stats map[string]uint64) uint64 {
	cache, ok := stats["inactive_file"]
	if !ok || cache == 0 {
		cache = stats["cache"]
	}
	if usage > cache {
		return usage - cache
	}
	return 0
}
