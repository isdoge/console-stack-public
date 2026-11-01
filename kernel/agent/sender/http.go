package sender

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"console-agent/collector"
	"console-agent/config"
)

// Identity 是 enrol 持久化的机器身份：machine_id + 上报密钥。
// 密钥由 Console 接收端签发（Bearer Token），存本地 state 目录（0600）。
type Identity struct {
	MachineID  string    `json:"machine_id"`
	Secret     string    `json:"secret"`
	EnrolledAt time.Time `json:"enrolled_at"`
}

// IdentityPath 返回 state 目录下的身份文件路径。
func IdentityPath(stateDir string) string {
	return filepath.Join(stateDir, "identity.json")
}

// LoadIdentity 读取本地身份；不存在时返回 nil（首次运行）。
func LoadIdentity(stateDir string) (*Identity, error) {
	data, err := os.ReadFile(IdentityPath(stateDir))
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var id Identity
	if err := json.Unmarshal(data, &id); err != nil {
		return nil, fmt.Errorf("解析身份文件: %w", err)
	}
	return &id, nil
}

// SaveIdentity 原子写身份文件（0600，仅 root 可读）。
func SaveIdentity(stateDir string, id *Identity) error {
	if err := os.MkdirAll(stateDir, 0o700); err != nil {
		return err
	}
	data, err := json.Marshal(id)
	if err != nil {
		return err
	}
	tmp := IdentityPath(stateDir) + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, IdentityPath(stateDir))
}

// EnrollRequest / EnrollResponse 与接收端 enrol 端点的契约形状。
type (
	EnrollRequest struct {
		MachineID       string `json:"machine_id"`
		EnrollToken     string `json:"enroll_token"`
		AgentVersion    string `json:"agent_version"`
		ProtocolVersion string `json:"protocol_version"`
	}
	EnrollResponse struct {
		Secret   string `json:"secret"`
		Enrolled bool   `json:"enrolled"`
	}
)

// ErrVersionRejected 表示接收端认为协议版本越界（HTTP 426）。
var ErrVersionRejected = errors.New("协议版本被拒收（426）")

// Client 是上报 HTTP 客户端。
type Client struct {
	base     string
	hc       *http.Client
	mu       sync.Mutex
	identity *Identity
}

// NewClient 创建上报客户端。base 是 Console 基址（无尾斜杠）。
func NewClient(base string, timeout time.Duration) *Client {
	return &Client{
		base: strings.TrimSuffix(base, "/"),
		hc:   &http.Client{Timeout: timeout},
	}
}

// SetIdentity 设置（或轮换后的）上报身份。
func (c *Client) SetIdentity(id *Identity) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.identity = id
}

// Identity 返回当前身份（可能为 nil）。
func (c *Client) Identity() *Identity {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.identity
}

func (c *Client) do(req *http.Request, body []byte) (*http.Response, error) {
	if body != nil {
		req.Body = io.NopCloser(bytes.NewReader(body))
		req.ContentLength = int64(len(body))
		req.Header.Set("Content-Type", "application/json")
	}
	req.Header.Set("User-Agent", "console-agent/"+config.Version)
	return c.hc.Do(req)
}

// Enroll 首次注册：向接收端申请密钥并持久化。
// 返回新身份。接收端 409（已 enrol）时重取身份失败，需要运维介入。
func (c *Client) Enroll(ctx context.Context, machineID, enrollToken, stateDir string) (*Identity, error) {
	if enrollToken == "" {
		return nil, errors.New("缺少 enrol-token（首次注册需要）")
	}
	reqBody, err := json.Marshal(EnrollRequest{
		MachineID:       machineID,
		EnrollToken:     enrollToken,
		AgentVersion:    config.Version,
		ProtocolVersion: config.ProtocolVersion,
	})
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.base+EnrollPath, nil)
	if err != nil {
		return nil, err
	}
	resp, err := c.do(req, reqBody)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusUpgradeRequired {
		return nil, ErrVersionRejected
	}
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusCreated {
		return nil, fmt.Errorf("enrol: %s", resp.Status)
	}
	var er EnrollResponse
	if err := json.NewDecoder(resp.Body).Decode(&er); err != nil {
		return nil, fmt.Errorf("解析 enrol 响应: %w", err)
	}
	if er.Secret == "" {
		return nil, errors.New("enrol 响应缺少 secret")
	}
	id := &Identity{MachineID: machineID, Secret: er.Secret, EnrolledAt: time.Now().UTC()}
	if err := SaveIdentity(stateDir, id); err != nil {
		return nil, fmt.Errorf("持久化身份: %w", err)
	}
	c.SetIdentity(id)
	return id, nil
}

// EnrollPath 与接收端约定（B4-3 实际路由：/api/monitoring/v1/enrol）。
const EnrollPath = "/api/monitoring/v1/enrol"

// Batch 是一次上报的请求体。Samples 的 SampledAt 保留真实采样时间（乱序补传合法）。
type Batch struct {
	Protocol          string              `json:"protocol"`
	AgentVersion      string              `json:"agent_version"`
	MachineID         string              `json:"machine_id"`
	SampleIntervalSec int                 `json:"sample_interval_sec"`
	Samples           []collector.Payload `json:"samples"`
}

// Send 批量上报样本。202/200 成功；426 版本越界；401 身份失效（需重 enrol 或运维）。
func (c *Client) Send(ctx context.Context, batch *Batch) error {
	id := c.Identity()
	if id == nil {
		return errors.New("未 enrol：缺少上报身份")
	}
	body, err := json.Marshal(batch)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.base+config.IngestPath, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+id.Secret)
	resp, err := c.do(req, body)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, resp.Body)
	switch {
	case resp.StatusCode == http.StatusAccepted || resp.StatusCode == http.StatusOK:
		return nil
	case resp.StatusCode == http.StatusUpgradeRequired:
		return ErrVersionRejected
	case resp.StatusCode == http.StatusUnauthorized:
		return fmt.Errorf("上报被拒（401）：身份失效或被吊销")
	default:
		return fmt.Errorf("上报失败: %s", resp.Status)
	}
}

// genSecret 生成本地 enrol 前置密钥材料（预留轮换流程）。
func genSecret() (string, error) {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return hex.EncodeToString(b), nil
}
