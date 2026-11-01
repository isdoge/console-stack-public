package sender

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"console-agent/collector"
	"console-agent/config"
)

// TestSendVersionNegotiation 覆盖版本协商：202 接受、426 拒收、401 身份失效。
func TestSendVersionNegotiation(t *testing.T) {
	dir := t.TempDir()

	// 接收端接受的样本记录（验证批次内容与 Bearer 头）。
	var gotBatch Batch
	var gotAuth string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		body, _ := io.ReadAll(r.Body)
		json.Unmarshal(body, &gotBatch)
		switch gotBatch.Protocol {
		case "v1":
			w.WriteHeader(http.StatusAccepted)
		case "v99":
			w.WriteHeader(http.StatusUpgradeRequired)
		default:
			w.WriteHeader(http.StatusBadRequest)
		}
	}))
	defer srv.Close()

	id := &Identity{MachineID: "nas", Secret: "s3cret", EnrolledAt: time.Now().UTC()}
	if err := SaveIdentity(dir, id); err != nil {
		t.Fatal(err)
	}
	reloaded, err := LoadIdentity(dir)
	if err != nil || reloaded == nil || reloaded.Secret != "s3cret" {
		t.Fatalf("身份持久化/加载失败: %v %+v", err, reloaded)
	}

	client := NewClient(srv.URL, 5*time.Second)
	client.SetIdentity(id)

	batch := &Batch{
		Protocol:          config.ProtocolVersion,
		AgentVersion:      config.Version,
		MachineID:         "nas",
		SampleIntervalSec: 300,
		Samples: []collector.Payload{
			{Protocol: "v1", MachineID: "nas", SampledAt: time.Now().UTC(), Seq: 1, Host: &collector.HostSample{CPUUsedPct: 3.2}},
		},
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	if err := client.Send(ctx, batch); err != nil {
		t.Fatalf("合法版本应被接受: %v", err)
	}
	if gotAuth != "Bearer s3cret" {
		t.Fatalf("Bearer 头错误: %q", gotAuth)
	}
	if gotBatch.AgentVersion != config.Version || gotBatch.MachineID != "nas" {
		t.Fatalf("批次字段错误: %+v", gotBatch)
	}
	if len(gotBatch.Samples) != 1 || gotBatch.Samples[0].Seq != 1 {
		t.Fatalf("样本丢失: %+v", gotBatch.Samples)
	}

	// 越界协议版本 → 426 → ErrVersionRejected。
	bad := *batch
	bad.Protocol = "v99"
	if err := client.Send(ctx, &bad); err == nil || err != ErrVersionRejected {
		t.Fatalf("越界版本应返回 ErrVersionRejected, got %v", err)
	}
}

// TestSendUnauthorized 覆盖 401（密钥吊销/失效）错误路径。
func TestSendUnauthorized(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer srv.Close()
	client := NewClient(srv.URL, 5*time.Second)
	client.SetIdentity(&Identity{MachineID: "nas", Secret: "x"})
	err := client.Send(context.Background(), &Batch{Protocol: config.ProtocolVersion, MachineID: "nas"})
	if err == nil {
		t.Fatal("401 应报错")
	}
}

// TestEnrollFlow 覆盖 enrol 全流程：请求形状、密钥持久化、后续身份加载。
func TestEnrollFlow(t *testing.T) {
	dir := t.TempDir()
	var gotReq EnrollRequest
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != EnrollPath {
			t.Errorf("enrol 路径错误: %s", r.URL.Path)
			http.NotFound(w, r)
			return
		}
		body, _ := io.ReadAll(r.Body)
		json.Unmarshal(body, &gotReq)
		w.WriteHeader(http.StatusCreated)
		json.NewEncoder(w).Encode(EnrollResponse{Secret: "issued-secret", Enrolled: true})
	}))
	defer srv.Close()

	client := NewClient(srv.URL, 5*time.Second)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	id, err := client.Enroll(ctx, "txy", "bootstrap-token", dir)
	if err != nil {
		t.Fatal(err)
	}
	if id.Secret != "issued-secret" {
		t.Fatalf("密钥错误: %q", id.Secret)
	}
	if gotReq.MachineID != "txy" || gotReq.EnrollToken != "bootstrap-token" {
		t.Fatalf("enrol 请求字段错误: %+v", gotReq)
	}
	if gotReq.AgentVersion != config.Version || gotReq.ProtocolVersion != config.ProtocolVersion {
		t.Fatalf("enrol 缺版本协商字段: %+v", gotReq)
	}
	// 身份已持久化且客户端已生效。
	if client.Identity().Secret != "issued-secret" {
		t.Fatal("客户端身份未生效")
	}
	reloaded, err := LoadIdentity(dir)
	if err != nil || reloaded.MachineID != "txy" {
		t.Fatalf("身份文件错误: %v %+v", err, reloaded)
	}
}

// TestEnrollRejectedByProtocol 覆盖 enrol 阶段的 426。
func TestEnrollRejectedByProtocol(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUpgradeRequired)
	}))
	defer srv.Close()
	client := NewClient(srv.URL, 5*time.Second)
	_, err := client.Enroll(context.Background(), "nas", "tok", t.TempDir())
	if err != ErrVersionRejected {
		t.Fatalf("期望 ErrVersionRejected, got %v", err)
	}
}

// TestSendRequiresIdentity 覆盖未 enrol 上报被本地拒绝。
func TestSendRequiresIdentity(t *testing.T) {
	client := NewClient("http://127.0.0.1:1", time.Second)
	if err := client.Send(context.Background(), &Batch{}); err == nil {
		t.Fatal("未 enrol 时 Send 应失败")
	}
}
