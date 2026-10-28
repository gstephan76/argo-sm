package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"os"
	"path/filepath"
	"testing"
)

func TestPropagateTraceHeaders(t *testing.T) {
	src := http.Header{}
	src.Set("Traceparent", "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
	src.Set("Tracestate", "vendor=value")
	src.Set("X-Request-Id", "req-123")
	src.Set("X-B3-Traceid", "aaaabbbbccccdddd")
	src.Set("B3", "aaaabbbbccccdddd-1234123412341234-1")
	src.Add("Baggage", "demo=a")
	src.Set("Authorization", "Bearer should-not-forward")
	dst := http.Header{}
	propagateTraceHeaders(dst, src)
	for _, name := range []string{"Traceparent", "Tracestate", "X-Request-Id", "X-B3-Traceid", "B3", "Baggage"} {
		if dst.Get(name) != src.Get(name) {
			t.Errorf("%s not forwarded: %q", name, dst.Get(name))
		}
	}
	if dst.Get("Authorization") != "" {
		t.Fatal("credentials must not be copied")
	}
}

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

// The Backend does not query SQL; it forwards the selected track to CRUD and
// stamps its own BLUE/YELLOW identity onto CRUD's database response.
func TestBackendForwardsToCRUD(t *testing.T) {
	crud := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("x-db-track"); got != "green" {
			t.Errorf("forwarded x-db-track = %q, want green", got)
		}
		if got := r.URL.Path; got != "/api/books" {
			t.Errorf("CRUD path = %q", got)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(response{
			App: "A", DatabaseTrack: "canary",
			DatabaseHost: "mariadb-a-canary.full-multi.svc.cluster.local",
			Books: []book{{ID: 1, Title: "Dom Casmurro", Author: "Machado de Assis", Year: 1899}},
		})
	}))
	defer crud.Close()
	s := &server{role: "backend", app: "A", prefix: "Application A - YELLOW",
		crudURL: crud.URL + "/api/books", client: crud.Client()}
	req := httptest.NewRequest(http.MethodGet, "/api/books", nil)
	req.Header.Set("x-db-track", "green")
	w := httptest.NewRecorder()
	s.routes().ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", w.Code, w.Body.String())
	}
	var got response
	if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if got.Backend != "Application A - YELLOW" || got.DatabaseTrack != "canary" || len(got.Books) != 1 {
		t.Fatalf("response: %+v", got)
	}
}


func TestReleaseColor(t *testing.T) {
	for _, tt := range []struct{ prefix, expected string }{
		{"Application A - BLUE", "BLUE"},
		{"Application B - YELLOW", "YELLOW"},
		{"Application A - CRUD", "UNKNOWN"},
	} {
		if got := releaseColor(tt.prefix); got != tt.expected { t.Fatalf("%q: %q", tt.prefix, got) }
	}
}

// The actual serving instances stamp their own identities, never infer
// identity from untrusted x-front-track / x-backend-track request headers.
func TestFrontAndBackendReleaseEvidence(t *testing.T) {
	crud := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("x-db-track") != "green" { t.Errorf("CRUD lost DB routing header") }
		_ = json.NewEncoder(w).Encode(response{App:"A", DatabaseTrack:"canary",
			DatabaseHost:"mariadb-a-canary.full-multi.svc.cluster.local",
			Books:[]book{{ID:1, Title:"Dom Casmurro", Author:"Machado de Assis",Year:1899}}})
	}))
	defer crud.Close()
	backend := &server{role:"backend", app:"A", prefix:"Application A - YELLOW", hostname:"backend-canary-pod", crudURL:crud.URL+"/api/books",client:crud.Client()}
	be := httptest.NewServer(backend.routes())
	defer be.Close()
	front := &server{role:"front", app:"A", prefix:"Application A - BLUE", hostname:"front-stable-pod",backendURL:be.URL+"/api/books",client:be.Client()}
	for _, path := range []string{"/api/books", "/"} {
		r := httptest.NewRequest(http.MethodGet,path,nil)
		r.Header.Set("x-db-track","green")
		r.Header.Set("x-front-track","canary") // deliberately wrong: serving Frontend is BLUE
		r.Header.Set("x-backend-track","stable") // deliberately wrong: serving Backend is YELLOW
		w := httptest.NewRecorder()
		front.routes().ServeHTTP(w,r)
		if w.Code != http.StatusOK { t.Fatalf("%s: %d %s",path,w.Code,w.Body.String()) }
		if got := w.Header().Get("X-Bookshelf-Frontend-Release"); got != "BLUE" { t.Fatalf("front header=%q",got) }
		if got := w.Header().Get("X-Bookshelf-Backend-Release"); got != "YELLOW" { t.Fatalf("backend header=%q",got) }
		if path == "/" {
			for _, want := range []string{"Frontend", "Backend", "BLUE RELEASE", "YELLOW RELEASE", "front-stable-pod", "backend-canary-pod", "Dom Casmurro", "class=\"card blue\"", "class=\"card yellow\""} {
				if !strings.Contains(w.Body.String(),want) { t.Fatalf("HTML missing %q",want) }
			}
			continue
		}
		var got response
		if err := json.Unmarshal(w.Body.Bytes(), &got);err != nil {t.Fatal(err)}
		if got.FrontendLabel != "BLUE RELEASE" || got.BackendLabel != "YELLOW RELEASE" || got.FrontendAccent != "#1755c9" || got.BackendAccent != "#d08b00" || got.FrontendRelease != "BLUE" || got.BackendRelease != "YELLOW" || got.FrontendPod != "front-stable-pod" || got.BackendPod != "backend-canary-pod" || got.DatabaseTrack != "canary" {t.Fatalf("JSON: %+v",got)}
	}
}


func TestConfigMapThemeLiveReload(t *testing.T) {
    path:=filepath.Join(t.TempDir(),"theme.json")
    s:=&server{themeFile:path}
    if s.theme().Blue.Label!="BLUE RELEASE" {t.Fatal("default theme missing")}
    initial:=[]byte(`{"blue":{"label":"CUSTOM BLUE","accent":"#113355","surface":"#ddeeff","ink":"#112233"}}`)
    if err:=os.WriteFile(path,initial,0600);err!=nil{t.Fatal(err)}
    if s.theme().Blue.Label!="CUSTOM BLUE" || s.theme().Blue.Accent!="#113355" {t.Fatal("ConfigMap not read")}
    modified:=[]byte(`{"blue":{"label":"UPDATED BLUE","accent":"url(javascript:alert(1))"}}`)
    if err:=os.WriteFile(path,modified,0600);err!=nil{t.Fatal(err)}
    if s.theme().Blue.Label!="UPDATED BLUE" {t.Fatal("ConfigMap live update not visible")}
    if s.theme().Blue.Accent!="#1755c9" {t.Fatal("unsafe CSS was accepted")}
}
