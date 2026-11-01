package collector

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"strings"
	"testing"
	"time"
)

func indexByte(s string, b byte) int { return strings.IndexByte(s, b) }

func TestParseStatsCPUAndMemory(t *testing.T) {
	raw := `{
  "read": "2026-10-24T12:07:12.689432699Z",
  "preread": "2026-10-24T12:07:11.689432699Z",
  "cpu_stats": {"cpu_usage": {"total_usage": 300}, "system_cpu_usage": 10000, "online_cpus": 2},
  "precpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 9000, "online_cpus": 2},
  "memory_stats": {"usage": 1000, "stats": {"inactive_file": 200}},
  "networks": {"eth0": {"rx_bytes": 100, "tx_bytes": 50}, "eth1": {"rx_bytes": 30, "tx_bytes": 20}}
}`
	var sj statsJSON
	if err := json.Unmarshal([]byte(raw), &sj); err != nil {
		t.Fatal(err)
	}
	st, err := parseStats(&sj)
	if err != nil {
		t.Fatal(err)
	}
	// cpuDelta=200, sysDelta=1000 → 200/1000*100 = 20%（不乘 online_cpus，beszel 口径）
	if st.CPUPct != 20 {
		t.Fatalf("CPU 百分比错误: %v（期望 20，整机容量口径不乘核数）", st.CPUPct)
	}
	if st.CPUWindowSec < 0.99 || st.CPUWindowSec > 1.01 {
		t.Fatalf("CPU 窗口错误: %v（期望 1s）", st.CPUWindowSec)
	}
	if st.MemoryUsedBytes != 800 {
		t.Fatalf("内存口径错误: %d（期望 usage-inactive=800）", st.MemoryUsedBytes)
	}
	if st.NetRecvBytesTotal != 130 || st.NetSentBytesTotal != 70 {
		t.Fatalf("网络累计错误: recv=%d sent=%d", st.NetRecvBytesTotal, st.NetSentBytesTotal)
	}
}

func TestParseStatsEdge(t *testing.T) {
	cases := []struct {
		name       string
		raw        string
		wantPct    float64
		wantWindow float64
	}{
		{
			name: "preread 为零值（daemon 无历史帧）→ 无有效窗",
			raw:  `{"read":"2026-10-24T12:00:00Z","preread":"0001-01-01T00:00:00Z","cpu_stats":{"cpu_usage":{"total_usage":100},"system_cpu_usage":1000},"precpu_stats":{"cpu_usage":{"total_usage":50},"system_cpu_usage":500}}`,
		},
		{
			name: "read == preread → 无有效窗",
			raw:  `{"read":"2026-10-24T12:00:00Z","preread":"2026-10-24T12:00:00Z","cpu_stats":{"cpu_usage":{"total_usage":100},"system_cpu_usage":1000},"precpu_stats":{"cpu_usage":{"total_usage":50},"system_cpu_usage":500}}`,
		},
		{
			name: "窗过短（<100ms）→ 无有效窗",
			raw:  `{"read":"2026-10-24T12:00:00.05Z","preread":"2026-10-24T12:00:00Z","cpu_stats":{"cpu_usage":{"total_usage":100},"system_cpu_usage":1000},"precpu_stats":{"cpu_usage":{"total_usage":50},"system_cpu_usage":500}}`,
		},
		{
			name: "precpu 相等（无差分基准）→ pct=0 但窗口有效",
			raw:  `{"read":"2026-10-24T12:00:01Z","preread":"2026-10-24T12:00:00Z","cpu_stats":{"cpu_usage":{"total_usage":100},"system_cpu_usage":1000},"precpu_stats":{"cpu_usage":{"total_usage":100},"system_cpu_usage":1000}}`,
			wantWindow: 1,
		},
		{
			name: "system 计数器回退（重置）→ pct=0 但窗口有效",
			raw:  `{"read":"2026-10-24T12:00:01Z","preread":"2026-10-24T12:00:00Z","cpu_stats":{"cpu_usage":{"total_usage":200},"system_cpu_usage":900},"precpu_stats":{"cpu_usage":{"total_usage":100},"system_cpu_usage":1000}}`,
			wantWindow: 1,
		},
		{
			name: "多核容器 40% 不被放大：cpuDelta=200 sysDelta=1000 → 20% 而非 40%×核数",
			raw:  `{"read":"2026-10-24T12:00:01Z","preread":"2026-10-24T12:00:00Z","cpu_stats":{"cpu_usage":{"total_usage":300},"system_cpu_usage":10000,"online_cpus":8},"precpu_stats":{"cpu_usage":{"total_usage":100},"system_cpu_usage":9000}}`,
			wantPct:    20,
			wantWindow: 1,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var sj statsJSON
			if err := json.Unmarshal([]byte(tc.raw), &sj); err != nil {
				t.Fatal(err)
			}
			st, err := parseStats(&sj)
			if err != nil {
				t.Fatal(err)
			}
			if st.CPUPct != tc.wantPct {
				t.Fatalf("CPU=%v 期望 %v", st.CPUPct, tc.wantPct)
			}
			if diff := st.CPUWindowSec - tc.wantWindow; diff < -0.01 || diff > 0.01 {
				t.Fatalf("窗口=%v 期望 %v", st.CPUWindowSec, tc.wantWindow)
			}
		})
	}
}

func TestParseStatsMemoryCacheFallback(t *testing.T) {
	cases := []struct {
		name string
		raw  string
		want uint64
	}{
		// beszel calculateMemoryUsage：inactive_file 优先，为 0 时回退 cache。
		{"inactive_file 在场", `{"memory_stats":{"usage":1000,"stats":{"inactive_file":200,"cache":999}}}`, 800},
		{"inactive_file 为 0 回退 cache", `{"memory_stats":{"usage":1000,"stats":{"inactive_file":0,"cache":250}}}`, 750},
		{"两者都缺席不减", `{"memory_stats":{"usage":1000,"stats":{}}}`, 1000},
		{"usage ≤ cache 不产生负值", `{"memory_stats":{"usage":100,"stats":{"inactive_file":200}}}`, 0},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var sj statsJSON
			if err := json.Unmarshal([]byte(tc.raw), &sj); err != nil {
				t.Fatal(err)
			}
			st, err := parseStats(&sj)
			if err != nil {
				t.Fatal(err)
			}
			if st.MemoryUsedBytes != tc.want {
				t.Fatalf("内存=%d 期望 %d", st.MemoryUsedBytes, tc.want)
			}
		})
	}
}

func TestHealthFromStatus(t *testing.T) {
	cases := map[string]string{
		"Up 2 hours":                       "none",
		"Up 2 hours (healthy)":             "healthy",
		"Up 30 seconds (health: starting)": "starting",
		"Up 1 minute (unhealthy)":          "unhealthy",
		"Exited (0) 5 minutes ago":         "none",
	}
	for in, want := range cases {
		if got := HealthFromStatus(in); got != want {
			t.Fatalf("HealthFromStatus(%q)=%q 期望 %q", in, got, want)
		}
	}
}

func TestCollectContainersOverHTTP(t *testing.T) {
	// 用本地 TCP listener 模拟 docker daemon 的 API 行为，
	// 覆盖 CollectContainers 编排（列表 + 并发 stats + 排序 + label/health 提取）全链路。
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	go http.Serve(l, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Path == "/containers/json" {
			fmt.Fprint(w, `[
 {"Id":"aaa111","Names":["/web"],"Image":"nginx:1","State":"running","Status":"Up 2 hours (healthy)","Labels":{"com.docker.compose.project":"webstack"}},
 {"Id":"bbb222","Names":["/db"],"Image":"postgres:16","State":"running","Status":"Up 3 hours","Labels":{}}
]`)
			return
		}
		if len(r.URL.Path) > len("/containers/") {
			rest := r.URL.Path[len("/containers/"):]
			id := rest
			if i := indexByte(rest, '/'); i >= 0 {
				id = rest[:i]
			}
			if id == "aaa111" {
				fmt.Fprint(w, `{"read":"2026-10-24T12:00:01Z","preread":"2026-10-24T12:00:00Z","cpu_stats":{"cpu_usage":{"total_usage":300},"system_cpu_usage":10000,"online_cpus":2},"precpu_stats":{"cpu_usage":{"total_usage":100},"system_cpu_usage":9000},"memory_stats":{"usage":1000,"stats":{"inactive_file":200}},"networks":{"eth0":{"rx_bytes":10,"tx_bytes":5}}}`)
			} else {
				fmt.Fprint(w, `{"read":"2026-10-24T12:00:01Z","preread":"2026-10-24T12:00:00Z","cpu_stats":{"cpu_usage":{"total_usage":50},"system_cpu_usage":1900,"online_cpus":1},"precpu_stats":{"cpu_usage":{"total_usage":40},"system_cpu_usage":900},"memory_stats":{"usage":500,"stats":{}},"networks":{}}`)
			}
			return
		}
		http.NotFound(w, r)
	}))

	client := NewDockerClient("", 5*time.Second)
	// 测试替身走 TCP：替换 transport 的 dial 目标。
	client.client.Transport = &http.Transport{
		DisableKeepAlives: true,
		Proxy:             func(*http.Request) (*url.URL, error) { return url.Parse("http://" + l.Addr().String()) },
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	samples, errs, err := CollectContainers(ctx, client)
	if err != nil {
		t.Fatal(err)
	}
	if len(samples) != 2 {
		t.Fatalf("容器数错误: %d", len(samples))
	}
	if len(errs) != 0 {
		t.Fatalf("不应有采集错误: %v", errs)
	}
	// 排序后 db 在前。
	if samples[0].Name != "db" || samples[1].Name != "web" {
		t.Fatalf("排序错误: %s, %s", samples[0].Name, samples[1].Name)
	}
	web := samples[1]
	if web.ComposeProject != "webstack" {
		t.Fatalf("compose 项目丢失: %q", web.ComposeProject)
	}
	if web.Health != "healthy" {
		t.Fatalf("health 解析错误: %q", web.Health)
	}
	if web.CPUPct != 20 {
		t.Fatalf("CPU 错误: %v（期望 20，不乘核数）", web.CPUPct)
	}
	if web.CPUDeltaSec < 0.99 || web.CPUDeltaSec > 1.01 {
		t.Fatalf("CPU 窗口元数据错误: %v（期望 1s）", web.CPUDeltaSec)
	}
	if web.MemoryUsedBytes != 800 {
		t.Fatalf("内存错误: %d", web.MemoryUsedBytes)
	}
	if web.NetRecvBytesTotal != 10 || web.NetSentBytesTotal != 5 {
		t.Fatalf("容器网络累计错误: recv=%d sent=%d", web.NetRecvBytesTotal, web.NetSentBytesTotal)
	}
}

// TestCollectContainersZeroPrereadMarksError 覆盖 daemon 无历史帧（one-shot
// 或冷启动）时：cpu_pct=0 不被当真实测量，errors 显式登记该容器。
func TestCollectContainersZeroPrereadMarksError(t *testing.T) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	go http.Serve(l, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Path == "/containers/json" {
			fmt.Fprint(w, `[{"Id":"ccc333","Names":["/cold"],"Image":"alpine","State":"running","Status":"Up 1 second"}]`)
			return
		}
		// preread 零值 = daemon 无差分历史。
		fmt.Fprint(w, `{"read":"2026-10-24T12:00:01Z","preread":"0001-01-01T00:00:00Z","cpu_stats":{"cpu_usage":{"total_usage":100},"system_cpu_usage":1000},"precpu_stats":{"cpu_usage":{"total_usage":0},"system_cpu_usage":0},"memory_stats":{"usage":100},"networks":{}}`)
	}))

	client := NewDockerClient("", 5*time.Second)
	client.client.Transport = &http.Transport{
		DisableKeepAlives: true,
		Proxy:             func(*http.Request) (*url.URL, error) { return url.Parse("http://" + l.Addr().String()) },
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	samples, errs, err := CollectContainers(ctx, client)
	if err != nil {
		t.Fatal(err)
	}
	if len(samples) != 1 {
		t.Fatalf("容器数错误: %d", len(samples))
	}
	if samples[0].CPUPct != 0 || samples[0].CPUDeltaSec != 0 {
		t.Fatalf("无历史帧应显式 0/无窗: pct=%v window=%v", samples[0].CPUPct, samples[0].CPUDeltaSec)
	}
	if _, ok := errs["docker:cold"]; !ok {
		t.Fatalf("无历史帧应记入 errors: %v", errs)
	}
}
