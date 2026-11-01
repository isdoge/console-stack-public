package config

import (
	"flag"
	"os"
	"path/filepath"
	"testing"
)

func TestLoadDefaults(t *testing.T) {
	// 无文件、无 env、无 flag：纯默认值（machine_id 必须显式给，无机器猜测默认）。
	c, err := loadFrom("/nonexistent/config.yaml", []string{"-machine-id", "nas"})
	if err != nil {
		t.Fatal(err)
	}
	if c.SampleIntervalDur().String() != "5m0s" {
		t.Fatalf("默认采样间隔错误: %v", c.SampleIntervalDur())
	}
	if c.BufferMax() != 100<<20 {
		t.Fatalf("默认缓冲上限错误: %d", c.BufferMax())
	}
	if c.StateDir != "/var/lib/console-agent" {
		t.Fatalf("默认 state 目录错误: %s", c.StateDir)
	}
	if c.DiskPath != "/" {
		t.Fatalf("默认磁盘路径错误: %s", c.DiskPath)
	}
}

func TestLoadLayeredOverride(t *testing.T) {
	dir := t.TempDir()
	confPath := filepath.Join(dir, "config.yaml")

	// 文件层：machine_id + server。
	os.WriteFile(confPath, []byte(`{"machine_id":"nas","server_url":"http://10.0.0.1:8081","sample_interval_sec":120}`), 0o600)
	c, err := loadFrom(confPath, nil)
	if err != nil {
		t.Fatal(err)
	}
	if c.MachineID != "nas" || c.ServerURL != "http://10.0.0.1:8081" {
		t.Fatalf("文件层未生效: %+v", c)
	}
	if c.SampleIntervalDur().String() != "2m0s" {
		t.Fatalf("文件层采样间隔未生效: %v", c.SampleIntervalDur())
	}

	// 环境变量层：覆盖 server。
	t.Setenv("CONSOLE_AGENT_SERVER", "http://10.0.0.2:8081")
	c, err = loadFrom(confPath, nil)
	if err != nil {
		t.Fatal(err)
	}
	if c.ServerURL != "http://10.0.0.2:8081" {
		t.Fatalf("env 层未生效: %s", c.ServerURL)
	}

	// flag 层：显式 flag 覆盖 env。
	c, err = loadFrom(confPath, []string{"-server", "http://10.0.0.3:8081/", "-sample-interval", "60"})
	if err != nil {
		t.Fatal(err)
	}
	if c.ServerURL != "http://10.0.0.3:8081" {
		t.Fatalf("flag 层未生效或尾斜杠未修剪: %s", c.ServerURL)
	}
	if c.SampleIntervalDur().String() != "1m0s" {
		t.Fatalf("flag 采样间隔未生效: %v", c.SampleIntervalDur())
	}
}

func TestLoadDiskPathOverride(t *testing.T) {
	dir := t.TempDir()
	confPath := filepath.Join(dir, "config.yaml")

	// 文件层：disk_path。
	os.WriteFile(confPath, []byte(`{"machine_id":"nas","disk_path":"/vol1"}`), 0o600)
	c, err := loadFrom(confPath, nil)
	if err != nil {
		t.Fatal(err)
	}
	if c.DiskPath != "/vol1" {
		t.Fatalf("文件层 disk_path 未生效: %s", c.DiskPath)
	}

	// env 层覆盖文件层。
	t.Setenv("CONSOLE_AGENT_DISK_PATH", "/vol2")
	c, err = loadFrom(confPath, nil)
	if err != nil {
		t.Fatal(err)
	}
	if c.DiskPath != "/vol2" {
		t.Fatalf("env 层 disk_path 未生效: %s", c.DiskPath)
	}

	// flag 层覆盖 env 层。
	c, err = loadFrom(confPath, []string{"-disk-path", "/data"})
	if err != nil {
		t.Fatal(err)
	}
	if c.DiskPath != "/data" {
		t.Fatalf("flag 层 disk_path 未生效: %s", c.DiskPath)
	}
}

func TestLoadDiskPathEmptyFallsBackToDefault(t *testing.T) {
	dir := t.TempDir()
	confPath := filepath.Join(dir, "config.yaml")

	// 空字符串回落默认 /（无 env/flag 干扰）。
	os.WriteFile(confPath, []byte(`{"machine_id":"nas","disk_path":""}`), 0o600)
	c, err := loadFrom(confPath, nil)
	if err != nil {
		t.Fatal(err)
	}
	if c.DiskPath != "/" {
		t.Fatalf("空 disk_path 应回落默认 /: %s", c.DiskPath)
	}
}

// loadFrom 是 Load 的测试辅助：指定配置文件路径与额外 flag。
func loadFrom(path string, extra []string) (*Config, error) {
	args := append([]string{"-config", path}, extra...)
	return Load(flag.NewFlagSet("t", flag.ContinueOnError), args)
}

func TestValidateRejects(t *testing.T) {
	cases := []Config{
		{ServerURL: "", MachineID: "nas", StateDir: "/tmp/x", sampleSecond: 300, bufferByte: 1},
		{ServerURL: "ftp://x", MachineID: "nas", StateDir: "/tmp/x", sampleSecond: 300, bufferByte: 1},
		{ServerURL: "http://x", MachineID: "", StateDir: "/tmp/x", sampleSecond: 300, bufferByte: 1},
		{ServerURL: "http://x", MachineID: "na s", StateDir: "/tmp/x", sampleSecond: 300, bufferByte: 1},
		{ServerURL: "http://x", MachineID: "nas", StateDir: "", sampleSecond: 300, bufferByte: 1},
		{ServerURL: "http://x", MachineID: "nas", StateDir: "/tmp/x", sampleSecond: 0, bufferByte: 1},
		{ServerURL: "http://x", MachineID: "nas", StateDir: "/tmp/x", sampleSecond: 300, bufferByte: 0},
	}
	for i, c := range cases {
		if err := c.Validate(); err == nil {
			t.Fatalf("case %d 应被拒绝: %+v", i, c)
		}
	}
}

func TestMissingFileOK(t *testing.T) {
	// 配置文件不存在不报错（纯 flag/env 运行形态）。
	c, err := loadFrom("/nonexistent/path/config.yaml", []string{"-machine-id", "txy"})
	if err != nil {
		t.Fatal(err)
	}
	if c.ServerURL == "" {
		t.Fatal("默认 server 应存在")
	}
}
