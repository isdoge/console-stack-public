package sender

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

func hashFile(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

// TestUpgradeHashReject 覆盖：下载内容与期望哈希不符时拒绝安装（文件删除、报 ErrHashMismatch）。
func TestUpgradeHashReject(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("tampered binary content"))
	}))
	defer srv.Close()

	dir := t.TempDir()
	up := &Upgrade{Dir: dir, Client: srv.Client()}
	path, err := up.Download(srv.URL+"/pkg", hashOf("different-content"))
	if err == nil {
		t.Fatal("哈希不符应报错")
	}
	if path != "" {
		t.Fatalf("失败时不应返回路径: %s", path)
	}
	if _, err := os.Stat(filepath.Join(dir, "console-agent.new")); !os.IsNotExist(err) {
		t.Fatal("校验失败的包应被删除")
	}
}

// TestUpgradeDownloadAndVerify 覆盖：哈希一致时包可用。
func TestUpgradeDownloadAndVerify(t *testing.T) {
	content := []byte("fake binary v0.2.0")
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write(content)
	}))
	defer srv.Close()

	want := sha256.Sum256(content)
	dir := t.TempDir()
	up := &Upgrade{Dir: dir, Client: srv.Client()}
	path, err := up.Download(srv.URL+"/pkg", hex.EncodeToString(want[:]))
	if err != nil {
		t.Fatal(err)
	}
	if path == "" {
		t.Fatal("应返回下载路径")
	}
	if err := VerifyHash(path, hex.EncodeToString(want[:])); err != nil {
		t.Fatalf("已下载包校验失败: %v", err)
	}
}

// TestApplyRollback 覆盖：升级后自检失败时回退到备份（旧版本继续可用）。
func TestApplyRollback(t *testing.T) {
	dir := t.TempDir()
	current := filepath.Join(dir, "console-agent")
	os.WriteFile(current, []byte("old-binary"), 0o755)
	newBin := filepath.Join(dir, "new.bin")
	os.WriteFile(newBin, []byte("broken-new-binary"), 0o755)

	up := &Upgrade{Dir: filepath.Join(dir, "upgrade")}
	// 自检失败 → 回退。
	err := up.Apply(newBin, current, func() error { return errors.New("self-check failed") })
	if err == nil {
		t.Fatal("自检失败应报错")
	}
	got, _ := os.ReadFile(current)
	if string(got) != "old-binary" {
		t.Fatalf("回退失败: %q", string(got))
	}

	// 自检成功 → 新版本就位、备份清理。
	err = up.Apply(newBin, current, func() error { return nil })
	if err != nil {
		t.Fatal(err)
	}
	got, _ = os.ReadFile(current)
	if string(got) != "broken-new-binary" {
		t.Fatalf("升级未生效: %q", string(got))
	}
	if _, err := os.Stat(filepath.Join(dir, "upgrade", "console-agent.bak")); !os.IsNotExist(err) {
		t.Fatal("成功后备份应清理")
	}
}

func hashOf(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])
}
