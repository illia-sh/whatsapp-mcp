package main

import (
	"testing"

	waLog "go.mau.fi/whatsmeow/util/log"
)

func TestResolveStoreDir(t *testing.T) {
	tests := []struct {
		name string
		env  string
		want string
	}{
		{"unset falls back to default", "", defaultStoreDir},
		{"relative path", "store-work", "store-work"},
		{"absolute path", "/tmp/wa/store", "/tmp/wa/store"},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Setenv("WHATSAPP_STORE_DIR", tt.env)
			if got := resolveStoreDir(); got != tt.want {
				t.Errorf("resolveStoreDir() = %q, want %q", got, tt.want)
			}
		})
	}
}

func TestResolveBridgePort(t *testing.T) {
	tests := []struct {
		name string
		env  string
		want int
	}{
		{"unset falls back to default", "", defaultBridgePort},
		{"valid port", "8081", 8081},
		{"lowest valid port", "1", 1},
		{"highest valid port", "65535", 65535},
		{"not a number falls back", "http://localhost:8081", defaultBridgePort},
		{"zero falls back", "0", defaultBridgePort},
		{"negative falls back", "-1", defaultBridgePort},
		{"above range falls back", "65536", defaultBridgePort},
	}

	logger := waLog.Noop
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Setenv("WHATSAPP_BRIDGE_PORT", tt.env)
			if got := resolveBridgePort(logger); got != tt.want {
				t.Errorf("resolveBridgePort() = %d, want %d", got, tt.want)
			}
		})
	}
}
