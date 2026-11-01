// Package collector 采集主机与容器指标。
//
// payload.go 定义上报单元的稳定 schema：单位后缀自描述（_pct/_bytes/_sec），
// 缺失一律显式（字段缺席或 health="none"），0 只出现在 0 是合法测量值的字段上。
package collector

import "time"

// Payload 是一次采样的完整单元，序列化为 JSONL 缓冲行，也是上报批次的样本成员。
type Payload struct {
	Protocol  string    `json:"protocol"` // 固定 "v1"
	Agent     string    `json:"agent"`    // agent 版本，如 "0.1.0"
	MachineID string    `json:"machine_id"`
	SampledAt time.Time `json:"sampled_at"` // 源机器实际采样时间（ISO Z）；不可变
	Seq       uint64    `json:"seq"`        // 跨重启单调递增，接收端幂等去重的辅助键

	Host       *HostSample       `json:"host,omitempty"`       // 主机缺失 = 整组采集失败
	Containers []ContainerSample `json:"containers,omitempty"` // 无 docker 能力时缺席

	// DroppedTotal 是本机因缓冲溢出累计丢弃的样本数（单调计数，随样本上报，缺口显式化）。
	DroppedTotal uint64 `json:"dropped_total,omitempty"`
	// Errors 记录本轮部分采集失败的摘要（键为采集器名），不携带敏感信息。
	Errors map[string]string `json:"errors,omitempty"`
}

// HostSample 是主机指标。累计计数器单调递增，机器重启归零（消费方按归零规则处理）。
type HostSample struct {
	CPUUsedPct        float64 `json:"cpu_used_pct"` // %，采样窗内均值（0–100）
	MemTotalBytes     uint64  `json:"mem_total_bytes"`
	MemAvailableBytes uint64  `json:"mem_available_bytes"`
	MemUsedBytes      uint64  `json:"mem_used_bytes"` // total - available
	SwapTotalBytes    uint64  `json:"swap_total_bytes"`
	SwapUsedBytes     uint64  `json:"swap_used_bytes"`
	DiskTotalBytes    uint64  `json:"disk_total_bytes"` // 根文件系统
	DiskUsedBytes     uint64  `json:"disk_used_bytes"`
	DiskUsedPct       float64 `json:"disk_used_pct"` // %
	LoadAvg1m         float64 `json:"load_avg_1m"`
	LoadAvg5m         float64 `json:"load_avg_5m"`
	LoadAvg15m        float64 `json:"load_avg_15m"`
	UptimeSec         uint64  `json:"uptime_sec"`

	// NICs 是逐网卡累计计数器；速率由 agent 用相邻样本差分计算（Rates）。
	NICs map[string]NICCounters `json:"nics,omitempty"`
	// Rates 是相对上一份成功样本的差分速率；首份样本或计数器归零后缺席。
	Rates *NICRates `json:"rates,omitempty"`
}

// NICCounters 是单网卡累计计数器（bytes，单调；重启归零）。
type NICCounters struct {
	SentBytesTotal uint64 `json:"sent_bytes_total"`
	RecvBytesTotal uint64 `json:"recv_bytes_total"`
}

// NICRates 是逐网卡差分速率（bytes/s）。
type NICRates struct {
	UpBytesSec   map[string]float64 `json:"up_bytes_sec,omitempty"`
	DownBytesSec map[string]float64 `json:"down_bytes_sec,omitempty"`
}

// ContainerSample 是单个容器的资源读数。
// 跨源稳定键 = machine_id + name（容器重建沿用同名即同一逻辑容器）；
// ID 是 Docker 容器 ID（重建即变），仅排障用。
type ContainerSample struct {
	Name           string `json:"name"`
	ID             string `json:"id"`
	Image          string `json:"image,omitempty"`
	State          string `json:"state,omitempty"`           // running/exited/...（Docker 原文）
	StatusText     string `json:"status_text,omitempty"`     // "Up 2 hours (healthy)" 原文，排障用
	Health         string `json:"health"`                    // none/starting/healthy/unhealthy
	ComposeProject string `json:"compose_project,omitempty"` // Docker label，缺失即未分组

	CPUPct            float64 `json:"cpu_pct"` // %（100% = 全部算力，beszel 同口径）
	CPUDeltaSec       float64 `json:"cpu_delta_sec,omitempty"` // CPU 差分窗秒数；0 = 无有效窗（cpu_pct 无测量意义）
	MemoryUsedBytes   uint64  `json:"memory_used_bytes"`
	NetSentBytesTotal uint64  `json:"net_sent_bytes_total,omitempty"` // 容器全部网卡累计
	NetRecvBytesTotal uint64  `json:"net_recv_bytes_total,omitempty"`
}
