package sender

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
)

// Upgrade 负责升级包的下载、SHA256 校验与失败回退。
// 升级路径：下载新二进制 → 校验哈希 → 原子替换 → 失败时回退到备份。
type Upgrade struct {
	// Dir 是升级工作目录（备份与下载暂存）。
	Dir string
	// Client 下载升级包用的 HTTP 客户端。
	Client *http.Client
}

// ErrHashMismatch 表示升级包哈希与期望不符，拒绝安装。
var ErrHashMismatch = errors.New("升级包 SHA256 校验失败")

// FileHash 计算文件的 SHA256（hex 小写）。纯函数，测试直接覆盖。
func FileHash(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// VerifyHash 校验文件哈希是否与期望一致（期望值为 hex 小写）。
func VerifyHash(path, want string) error {
	got, err := FileHash(path)
	if err != nil {
		return err
	}
	if got != want {
		return fmt.Errorf("%w: got %s want %s", ErrHashMismatch, got, want)
	}
	return nil
}

// Download 下载升级包到工作目录并校验哈希。校验失败删除文件并返回 ErrHashMismatch。
func (u *Upgrade) Download(url, wantHash string) (string, error) {
	if err := os.MkdirAll(u.Dir, 0o700); err != nil {
		return "", err
	}
	resp, err := u.Client.Get(url)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("下载升级包: %s", resp.Status)
	}
	tmp := filepath.Join(u.Dir, "console-agent.new")
	out, err := os.OpenFile(tmp, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o755)
	if err != nil {
		return "", err
	}
	if _, err := io.Copy(out, resp.Body); err != nil {
		out.Close()
		os.Remove(tmp)
		return "", err
	}
	out.Close()
	if err := VerifyHash(tmp, wantHash); err != nil {
		os.Remove(tmp)
		return "", err
	}
	return tmp, nil
}

// Apply 用已校验的新二进制替换 current：先备份、再原子替换；
// 替换后运行 verifyCmd 自检，失败则回退备份。
func (u *Upgrade) Apply(newBin, current string, verifyCmd func() error) error {
	if err := os.MkdirAll(u.Dir, 0o700); err != nil {
		return err
	}
	backup := filepath.Join(u.Dir, "console-agent.bak")
	// 备份当前版本（当前文件不存在 = 首次安装，跳过备份）。
	if _, err := os.Stat(current); err == nil {
		if err := copyFile(current, backup); err != nil {
			return fmt.Errorf("备份当前版本: %w", err)
		}
	} else if !os.IsNotExist(err) {
		return err
	}
	if err := copyFile(newBin, current+".tmp"); err != nil {
		return err
	}
	if err := os.Chmod(current+".tmp", 0o755); err != nil {
		return err
	}
	if err := os.Rename(current+".tmp", current); err != nil {
		return err
	}
	if verifyCmd != nil {
		if err := verifyCmd(); err != nil {
			// 自检失败：回退备份。
			if bakErr := copyFile(backup, current); bakErr == nil {
				os.Chmod(current, 0o755)
			}
			return fmt.Errorf("升级后自检失败，已回退: %w", err)
		}
	}
	os.Remove(backup)
	return nil
}

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o755)
	if err != nil {
		return err
	}
	defer out.Close()
	if _, err := io.Copy(out, in); err != nil {
		return err
	}
	return nil
}
