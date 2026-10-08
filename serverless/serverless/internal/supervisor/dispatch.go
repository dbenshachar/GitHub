package supervisor

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

func RemoteDispatcher(url, token string) func(context.Context, Spec, int) (*Result, error) {
	client := &http.Client{Transport: &http.Transport{MaxIdleConns: 1024, MaxIdleConnsPerHost: 1024, IdleConnTimeout: 30 * time.Second}}
	return func(ctx context.Context, s Spec, depth int) (*Result, error) {
		b, _ := json.Marshal(submission{Spec: s, Count: 1, Depth: depth})
		req, err := http.NewRequestWithContext(ctx, "POST", url+"/v1/invoke", bytes.NewReader(b))
		if err != nil {
			return nil, err
		}
		req.Header.Set("Authorization", "Bearer "+token)
		req.Header.Set("Content-Type", "application/json")
		resp, err := client.Do(req)
		if err != nil {
			return nil, err
		}
		defer resp.Body.Close()
		if resp.StatusCode != 200 {
			b, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
			return nil, fmt.Errorf("child invocation HTTP %d: %s", resp.StatusCode, b)
		}
		var r Result
		err = json.NewDecoder(io.LimitReader(resp.Body, 4*1024*1024)).Decode(&r)
		return &r, err
	}
}
