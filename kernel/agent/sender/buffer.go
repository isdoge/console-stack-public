// Package sender 实现上报链路：本地缓冲（断网暂存、按真实采样时间补传）
// 与 HTTP 发送（身份认证、版本协商、批处理）。
package sender

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"

	"console-agent/collector"
)

// ErrBufferFull 表示缓冲已达字节上限，新样本被拒（由调用方计数丢弃、随下一样本上报）。
var ErrBufferFull = errors.New("buffer: 缓冲已达上限")

// Buffer 是 JSONL 暂存队列：断网时写入本地文件，恢复后按行补传。
// 行序即入队序；溢出时丢弃队头（最旧）样本——最旧的补传价值最低，
// 且丢弃计数显式上报（Payload.DroppedTotal），缺口不静默。
type Buffer struct {
	mu       sync.Mutex
	path     string
	maxBytes int64
	dropped  uint64 // 因溢出累计丢弃的样本数（单调）
	size     int64  // 当前文件字节数（含换行）
}

// OpenBuffer 打开（或创建）state 目录下的 buffer JSONL 文件，并做损坏行修复。
func OpenBuffer(stateDir string, maxBytes int64) (*Buffer, error) {
	if err := os.MkdirAll(stateDir, 0o700); err != nil {
		return nil, fmt.Errorf("创建状态目录: %w", err)
	}
	path := filepath.Join(stateDir, "buffer.jsonl")
	b := &Buffer{path: path, maxBytes: maxBytes}
	if err := b.compact(); err != nil {
		return nil, err
	}
	return b, nil
}

// compact 扫描现有文件，重写为只含完整合法行的形状。
// 半行（进程崩溃时写了一半）截断丢弃；无法解析的行删除（按丢弃计数）。
func (b *Buffer) compact() error {
	f, err := os.Open(b.path)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	defer f.Close()

	var kept bytes.Buffer
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 64*1024), 4<<20)
	bad := 0
	for sc.Scan() {
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		var p collector.Payload
		if json.Unmarshal(line, &p) != nil || p.MachineID == "" || p.SampledAt.IsZero() {
			bad++
			continue
		}
		kept.Write(line)
		kept.WriteByte('\n')
	}
	if err := sc.Err(); err != nil {
		return fmt.Errorf("扫描缓冲文件: %w", err)
	}
	if bad == 0 && int64(kept.Len()) == b.sizeOf() {
		return nil // 无损坏，免写回
	}
	b.dropped += uint64(bad)
	return b.writeAll(kept.Bytes())
}

func (b *Buffer) sizeOf() int64 {
	fi, err := os.Stat(b.path)
	if err != nil {
		return 0
	}
	return fi.Size()
}

func (b *Buffer) writeAll(data []byte) error {
	if err := os.WriteFile(b.path+".tmp", data, 0o600); err != nil {
		return err
	}
	if err := os.Rename(b.path+".tmp", b.path); err != nil {
		return err
	}
	b.size = int64(len(data))
	return nil
}

// Push 把样本追加到缓冲（若在线则随后立即被取走）。超限时丢弃队头腾位。
func (b *Buffer) Push(p *collector.Payload) error {
	line, err := json.Marshal(p)
	if err != nil {
		return err
	}
	line = append(line, '\n')

	b.mu.Lock()
	defer b.mu.Unlock()
	if b.size == 0 && b.sizeOf() > 0 {
		b.size = b.sizeOf()
	}
	for b.size+int64(len(line)) > b.maxBytes {
		if err := b.dropOldestLocked(); err != nil {
			return err
		}
	}
	f, err := os.OpenFile(b.path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	defer f.Close()
	if _, err := f.Write(line); err != nil {
		return err
	}
	b.size += int64(len(line))
	return nil
}

// dropOldestLocked 删除文件第一行。文件为空时是 no-op。
func (b *Buffer) dropOldestLocked() error {
	data, err := os.ReadFile(b.path)
	if err != nil {
		if os.IsNotExist(err) {
			b.size = 0
			return nil
		}
		return err
	}
	rest := bytes.SplitN(data, []byte("\n"), 2)
	if len(rest) < 2 {
		b.size = 0
		return b.writeAll(nil)
	}
	b.dropped++
	return b.writeAll(rest[1])
}

// Take 取出至多 max 条样本（保持入队序）。取出的行写回 pending 文件，
// 确认（Ack）后删除；未确认的行在下次 Take 前 restore 回队头。
func (b *Buffer) Take(max int) ([]collector.Payload, error) {
	b.mu.Lock()
	defer b.mu.Unlock()

	// 未确认的 pending 行先回队头，保证不丢。
	if err := b.restoreLocked(); err != nil {
		return nil, err
	}
	lines, err := b.readLinesLocked(max)
	if err != nil || len(lines) == 0 {
		return nil, err
	}
	var payloads []collector.Payload
	for _, line := range lines {
		var p collector.Payload
		if json.Unmarshal(line, &p) == nil {
			payloads = append(payloads, p)
		}
	}
	// 截断已取出的行。
	remaining := b.linesAfterLocked(max)
	if err := b.writeAll(remaining); err != nil {
		return nil, err
	}
	// pending 副本（原子写）。
	blob, _ := json.Marshal(payloads)
	if err := os.WriteFile(b.path+".pending.tmp", blob, 0o600); err != nil {
		return nil, err
	}
	if err := os.Rename(b.path+".pending.tmp", b.path+".pending"); err != nil {
		return nil, err
	}
	return payloads, nil
}

// Ack 确认 pending 行已成功上报，删除 pending 文件。
func (b *Buffer) Ack() error {
	b.mu.Lock()
	defer b.mu.Unlock()
	err := os.Remove(b.path + ".pending")
	if err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}

// restoreLocked 把未确认的 pending 行放回队头。
func (b *Buffer) restoreLocked() error {
	blob, err := os.ReadFile(b.path + ".pending")
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	var pending []collector.Payload
	if json.Unmarshal(blob, &pending) != nil {
		// pending 坏了：宁可重复上报（接收端幂等去重）也不丢数据。
		return os.Remove(b.path + ".pending")
	}
	data, err := os.ReadFile(b.path)
	if err != nil && !os.IsNotExist(err) {
		return err
	}
	var buf bytes.Buffer
	for i := range pending {
		line, err := json.Marshal(&pending[i])
		if err != nil {
			continue
		}
		buf.Write(line)
		buf.WriteByte('\n')
	}
	buf.Write(data)
	return b.writeAll(buf.Bytes())
}

func (b *Buffer) readLinesLocked(max int) ([][]byte, error) {
	data, err := os.ReadFile(b.path)
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var lines [][]byte
	for _, line := range bytes.Split(data, []byte("\n")) {
		if len(line) > 0 {
			lines = append(lines, line)
		}
	}
	if max >= 0 && len(lines) > max {
		lines = lines[:max]
	}
	return lines, nil
}

func (b *Buffer) linesAfterLocked(skip int) []byte {
	data, err := os.ReadFile(b.path)
	if err != nil {
		return nil
	}
	parts := bytes.SplitN(data, []byte("\n"), skip+1)
	if len(parts) <= skip {
		return nil
	}
	return parts[skip]
}

// Len 返回当前缓冲的样本数（pending 不计）。
func (b *Buffer) Len() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	lines, err := b.readLinesLocked(-1)
	if err != nil {
		return 0
	}
	return len(lines)
}

// Dropped 返回溢出丢弃的累计计数。
func (b *Buffer) Dropped() uint64 {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.dropped
}

// MaxBytes 返回配置的字节上限。
func (b *Buffer) MaxBytes() int64 { return b.maxBytes }
