package sender

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"

	"console-agent/collector"
	"console-agent/config"
)

func mkPayload(seq uint64, at time.Time) *collector.Payload {
	return &collector.Payload{
		Protocol:  config.ProtocolVersion,
		Agent:     config.Version,
		MachineID: "nas",
		SampledAt: at,
		Seq:       seq,
		Host:      &collector.HostSample{CPUUsedPct: 12.5, UptimeSec: 1000},
	}
}

func TestBufferPushTakeAck(t *testing.T) {
	dir := t.TempDir()
	b, err := OpenBuffer(dir, 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	t0 := time.Now().UTC()
	for i := uint64(1); i <= 3; i++ {
		if err := b.Push(mkPayload(i, t0.Add(time.Duration(i)*time.Minute))); err != nil {
			t.Fatal(err)
		}
	}
	if b.Len() != 3 {
		t.Fatalf("Len=%d 期望 3", b.Len())
	}
	got, err := b.Take(2)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 {
		t.Fatalf("Take(2) 返回 %d", len(got))
	}
	// 保持入队序 + 真实采样时间。
	if got[0].Seq != 1 || got[1].Seq != 2 {
		t.Fatalf("顺序错误: %d, %d", got[0].Seq, got[1].Seq)
	}
	if !got[0].SampledAt.Equal(t0.Add(time.Minute)) {
		t.Fatalf("采样时间被改写: %v", got[0].SampledAt)
	}
	if b.Len() != 1 {
		t.Fatalf("Take 后 Len=%d 期望 1", b.Len())
	}
	if err := b.Ack(); err != nil {
		t.Fatal(err)
	}
	if b.Len() != 1 {
		t.Fatalf("Ack 后 Len=%d 期望 1（Ack 只清 pending，不动主文件）", b.Len())
	}
}

func TestBufferUnackedRestore(t *testing.T) {
	dir := t.TempDir()
	b, _ := OpenBuffer(dir, 1<<20)
	t0 := time.Now().UTC()
	b.Push(mkPayload(1, t0))
	b.Push(mkPayload(2, t0.Add(time.Minute)))

	got, _ := b.Take(10)
	if len(got) != 2 {
		t.Fatalf("Take 返回 %d", len(got))
	}
	// 不 Ack：下次 Take 前 pending 应回队头，样本不丢（宁可重复上报）。
	got2, err := b.Take(10)
	if err != nil {
		t.Fatal(err)
	}
	if len(got2) != 2 || got2[0].Seq != 1 {
		t.Fatalf("pending 恢复失败: %d 条, 首 seq=%d", len(got2), got2[0].Seq)
	}
	if err := b.Ack(); err != nil {
		t.Fatal(err)
	}
	if n := b.Len(); n != 0 {
		t.Fatalf("Ack 后残留 %d 条", n)
	}
}

func TestBufferOverflowDropsOldest(t *testing.T) {
	dir := t.TempDir()
	// 上限设为单条样本行大小 ×3：第 4 条进来时丢弃最旧一条。
	t0 := time.Now().UTC()
	probe, _ := json.Marshal(mkPayload(1, t0))
	limit := int64(len(probe)+1) * 3
	b, err := OpenBuffer(dir, limit)
	if err != nil {
		t.Fatal(err)
	}
	for i := uint64(1); i <= 4; i++ {
		if err := b.Push(mkPayload(i, t0)); err != nil {
			t.Fatal(err)
		}
	}
	if d := b.Dropped(); d != 1 {
		t.Fatalf("Dropped=%d 期望 1", d)
	}
	got, _ := b.Take(10)
	if len(got) != 3 {
		t.Fatalf("残留 %d 条", len(got))
	}
	if got[0].Seq != 2 || got[2].Seq != 4 {
		t.Fatalf("应保留最新三条: %d..%d", got[0].Seq, got[2].Seq)
	}
}

func TestBufferCompactRepairsHalfLine(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "buffer.jsonl")
	t0 := time.Now().UTC()
	good1, _ := json.Marshal(mkPayload(1, t0))
	good2, _ := json.Marshal(mkPayload(2, t0))
	// 两行完整 + 一行崩溃残留的半行（无换行）。
	content := append(append(append(good1, '\n'), append(good2, '\n')...), []byte(`{"protocol":"v1","machine_i`)...)
	if err := os.WriteFile(path, content, 0o600); err != nil {
		t.Fatal(err)
	}

	b, err := OpenBuffer(dir, 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	if n := b.Len(); n != 2 {
		t.Fatalf("compact 后应剩 2 条完整行, got %d", n)
	}
}

func TestBufferReopenKeepsState(t *testing.T) {
	dir := t.TempDir()
	b, _ := OpenBuffer(dir, 1<<20)
	t0 := time.Now().UTC()
	b.Push(mkPayload(1, t0))
	b.Push(mkPayload(2, t0))
	// 重新打开（进程重启语义）。
	b2, err := OpenBuffer(dir, 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	if n := b2.Len(); n != 2 {
		t.Fatalf("重启后样本丢失: %d", n)
	}
	got, _ := b2.Take(10)
	if len(got) != 2 || got[0].Seq != 1 {
		t.Fatalf("重启后顺序/内容错误")
	}
}
