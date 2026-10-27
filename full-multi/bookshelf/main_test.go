package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestTracks(t *testing.T) {
	h := http.Header{}
	if track(h, "x-db-track") != "stable" {
		t.Fatal("default")
	}
	h.Set("x-db-track", "canary")
	if track(h, "x-db-track") != "canary" {
		t.Fatal("canary")
	}
	h.Set("x-db-track", "other")
	if track(h, "x-db-track") != "stable" {
		t.Fatal("unexpected")
	}
}
func TestInfo(t *testing.T) {
	s := &server{role: "front", app: "A", prefix: "Application A - BLUE"}
	r := httptest.NewRequest("GET", "/info", nil)
	w := httptest.NewRecorder()
	s.routes().ServeHTTP(w, r)
	if !strings.Contains(w.Body.String(), "Application A - BLUE") {
		t.Fatal(w.Body.String())
	}
}
