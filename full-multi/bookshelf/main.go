// Header-aware Bookshelf demo: same image serves Front A/B and Backend A/B.
// MariaDB traffic is TCP; x-db-track is interpreted by Backend per request.
package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"html/template"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/go-sql-driver/mysql"
)

type book struct {
	ID     int    `json:"id"`
	Title  string `json:"title"`
	Author string `json:"author"`
	Year   int    `json:"year"`
}
type response struct {
	Books         []book `json:"books"`
	App           string `json:"application"`
	DatabaseTrack string `json:"database_track,omitempty"`
	DatabaseHost  string `json:"database_host,omitempty"`
	Backend       string `json:"backend,omitempty"`
	Frontend      string `json:"frontend,omitempty"`
	FrontendRelease string `json:"frontend_release,omitempty"`
	FrontendPod   string `json:"frontend_pod,omitempty"`
	BackendRelease string `json:"backend_release,omitempty"`
	BackendPod    string `json:"backend_pod,omitempty"`
	FrontendLabel string `json:"frontend_label,omitempty"`
	FrontendAccent string `json:"frontend_accent,omitempty"`
	BackendLabel string `json:"backend_label,omitempty"`
	BackendAccent string `json:"backend_accent,omitempty"`
}
type server struct {
	role, app, prefix, hostname, themeFile, backendURL, crudURL, dbStable, dbCanary, dbName, dbUser, dbPassword string
	client                                                                        *http.Client
}

func track(h http.Header, name string) string {
	if strings.EqualFold(strings.TrimSpace(h.Get(name)), "canary") {
		return "canary"
	}
	return "stable"
}
// Release color is an observed executable revision; colors and display labels
// are supplied by the projected ConfigMap, not by the incoming routing headers.
type releaseStyle struct {
    Label string `json:"label"`
    Subtitle string `json:"subtitle"`
    Accent string `json:"accent"`
    Surface string `json:"surface"`
    Ink string `json:"ink"`
}
type releaseTheme struct {
    Blue releaseStyle `json:"blue"`
    Yellow releaseStyle `json:"yellow"`
}
func defaultTheme() releaseTheme {
    return releaseTheme{
        Blue: releaseStyle{Label:"BLUE RELEASE",Subtitle:"Baseline software revision",Accent:"#1755c9",Surface:"#e7efff",Ink:"#123782"},
        Yellow: releaseStyle{Label:"YELLOW RELEASE",Subtitle:"Candidate software revision",Accent:"#d08b00",Surface:"#fff5d8",Ink:"#6a4800"},
    }
}
func validHexColor(s string) bool {
    if len(s)!=7 || s[0]!='#' {return false}
    for _, c := range s[1:] {
        if !((c>='0' && c<='9') || (c>='a' && c<='f') || (c>='A' && c<='F')) {return false}
    }
    return true
}
func mergeStyle(defaults, supplied releaseStyle) releaseStyle {
    if supplied.Label!="" && len(supplied.Label)<=80 {defaults.Label=supplied.Label}
    if supplied.Subtitle!="" && len(supplied.Subtitle)<=200 {defaults.Subtitle=supplied.Subtitle}
    if validHexColor(supplied.Accent) {defaults.Accent=supplied.Accent}
    if validHexColor(supplied.Surface) {defaults.Surface=supplied.Surface}
    if validHexColor(supplied.Ink) {defaults.Ink=supplied.Ink}
    return defaults
}
func (s *server) theme() releaseTheme {
    defaults:=defaultTheme()
    path:=s.themeFile
    if path=="" {path="/etc/bookshelf-theme/theme.json"}
    // Projected ConfigMap volumes update atomically; do not cache at startup.
    stat,err:=os.Stat(path)
    if err!=nil || stat.Size()>16*1024 {return defaults}
    data,err:=os.ReadFile(path)
    if err!=nil {return defaults}
    var cfg releaseTheme
    if json.Unmarshal(data,&cfg)!=nil {return defaults}
    defaults.Blue=mergeStyle(defaults.Blue,cfg.Blue)
    defaults.Yellow=mergeStyle(defaults.Yellow,cfg.Yellow)
    return defaults
}
func (t releaseTheme) forRelease(release string) releaseStyle {
    switch release {
        case "BLUE": return t.Blue
        case "YELLOW": return t.Yellow
        default: return releaseStyle{Label:"UNKNOWN", Subtitle:"Unrecognized revision",Accent:"#626b7b",Surface:"#eef0f3",Ink:"#384152"}
    }
}
func (t releaseTheme) css() template.CSS {
    // CSS-safe conversion: only strict #RRGGBB values are ever interpolated.
    return template.CSS(fmt.Sprintf(".card.blue{--accent:%s;--surface:%s;--ink:%s}.card.yellow{--accent:%s;--surface:%s;--ink:%s}",
        t.Blue.Accent,t.Blue.Surface,t.Blue.Ink,t.Yellow.Accent,t.Yellow.Surface,t.Yellow.Ink))
}

func releaseColor(prefix string) string {
	if strings.HasSuffix(prefix, " - BLUE") { return "BLUE" }
	if strings.HasSuffix(prefix, " - YELLOW") { return "YELLOW" }
	return "UNKNOWN"
}

func setReleaseHeaders(h http.Header, out response) {
	if out.FrontendRelease != "" { h.Set("X-Bookshelf-Frontend-Release", out.FrontendRelease) }
	if out.BackendRelease != "" { h.Set("X-Bookshelf-Backend-Release", out.BackendRelease) }
	if out.FrontendAccent != "" { h.Set("X-Bookshelf-Frontend-Color", out.FrontendAccent) }
	if out.BackendAccent != "" { h.Set("X-Bookshelf-Backend-Color", out.BackendAccent) }
}

func newServer() *server {
	return &server{role: os.Getenv("ROLE"), app: os.Getenv("APP_ID"), prefix: os.Getenv("MESSAGE_PREFIX"), hostname: os.Getenv("HOSTNAME"), themeFile: os.Getenv("BOOKSHELF_THEME_FILE"), backendURL: os.Getenv("BACKEND_API_URL"), crudURL: os.Getenv("CRUD_API_URL"), dbStable: os.Getenv("DB_HOST_STABLE"), dbCanary: os.Getenv("DB_HOST_CANARY"), dbName: os.Getenv("DB_NAME"), dbUser: os.Getenv("DB_USER"), dbPassword: os.Getenv("DB_PASSWORD"), client: &http.Client{Timeout: 8 * time.Second}}
}
func (s *server) routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) { fmt.Fprintln(w, "OK") })
	mux.HandleFunc("/info", func(w http.ResponseWriter, r *http.Request) { fmt.Fprintf(w, "%s %s\n", s.prefix, s.role) })
	mux.HandleFunc("/api/books", s.books)
	mux.HandleFunc("/", s.index)
	return mux
}

// Istio/Envoy requires the application to forward incoming trace context
// when it creates a new downstream HTTP request. Do not forward credentials.
var downstreamTraceHeaders = []string{
	"x-request-id", "traceparent", "tracestate", "baggage",
	"b3", "x-b3-traceid", "x-b3-spanid", "x-b3-parentspanid",
	"x-b3-sampled", "x-b3-flags", "x-ot-span-context",
}

func propagateTraceHeaders(dst, src http.Header) {
	for _, name := range downstreamTraceHeaders {
		for _, value := range src.Values(name) {
			dst.Add(name, value)
		}
	}
}

func (s *server) upstream(r *http.Request) ([]byte, int, error) {
	destination := s.backendURL
	if s.role == "backend" {
		destination = s.crudURL
	}
	u, err := url.Parse(destination)
	if err != nil {
		return nil, 0, err
	}
	u.RawQuery = r.URL.RawQuery
	req, err := http.NewRequestWithContext(r.Context(), http.MethodGet, u.String(), nil)
	if err != nil {
		return nil, 0, err
	}
	propagateTraceHeaders(req.Header, r.Header)
	for _, h := range []string{"x-backend-track", "x-db-track"} {
		if v := r.Header.Get(h); v != "" {
			req.Header.Set(h, v)
		}
	}
	req.Header.Set("x-app-target", strings.ToLower(s.app))
	resp, err := s.client.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	return b, resp.StatusCode, err
}
func (s *server) books(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", 405)
		return
	}
	if s.role == "front" || s.role == "backend" {
		b, status, err := s.upstream(r)
		if err != nil {
			http.Error(w, "downstream unreachable: "+err.Error(), http.StatusBadGateway)
			return
		}
		if status == http.StatusOK {
			var out response
			if err := json.Unmarshal(b, &out); err != nil {
				http.Error(w, "invalid downstream response", http.StatusBadGateway)
				return
			}
			if s.role == "backend" {
				out.Backend = s.prefix
				out.BackendRelease = releaseColor(s.prefix)
				out.BackendPod = s.hostname
				style:=s.theme().forRelease(out.BackendRelease)
				out.BackendLabel=style.Label
				out.BackendAccent=style.Accent
			} else {
				out.Frontend = s.prefix
				out.FrontendRelease = releaseColor(s.prefix)
				out.FrontendPod = s.hostname
				style:=s.theme().forRelease(out.FrontendRelease)
				out.FrontendLabel=style.Label
				out.FrontendAccent=style.Accent
			}
			b, err = json.Marshal(out)
			if err != nil {
				http.Error(w, "cannot encode downstream response", http.StatusInternalServerError)
				return
			}
			setReleaseHeaders(w.Header(), out)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write(b)
		return
	}
	if s.role != "crud" {
		http.Error(w, "invalid role", 500)
		return
	}
	// This internal header is overwritten by the CRUD Istio VirtualService.
	// x-db-track itself is intentionally not used to select an SQL endpoint.
	out, err := s.queryBooks(r.Context(), track(r.Header, "x-crud-db-target"))
	if err != nil {
		log.Printf("database query error: %s", err)
		http.Error(w, "database unavailable", 503)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(out)
}
func (s *server) queryBooks(ctx context.Context, t string) (response, error) {
	host := s.dbStable
	if t == "canary" {
		host = s.dbCanary
	}
	if host == "" {
		return response{}, errors.New("database host is missing")
	}
	cfg := mysql.NewConfig()
	cfg.User = s.dbUser
	cfg.Passwd = s.dbPassword
	cfg.Net = "tcp"
	cfg.Addr = host + ":3306"
	cfg.DBName = s.dbName
	cfg.ParseTime = true
	cfg.Timeout = 3 * time.Second
	cfg.ReadTimeout = 4 * time.Second
	cfg.WriteTimeout = 4 * time.Second
	db, err := sql.Open("mysql", cfg.FormatDSN())
	if err != nil {
		return response{}, err
	}
	defer db.Close()
	db.SetMaxOpenConns(2)
	db.SetConnMaxLifetime(15 * time.Second)
	c, cancel := context.WithTimeout(ctx, 7*time.Second)
	defer cancel()
	if err = db.PingContext(c); err != nil {
		return response{}, err
	}
	_, err = db.ExecContext(c, `CREATE TABLE IF NOT EXISTS books (id INT PRIMARY KEY, title VARCHAR(200) NOT NULL, author VARCHAR(200) NOT NULL, year INT NOT NULL)`)
	if err != nil {
		return response{}, err
	}
	for _, b := range []book{{1, "The Hobbit", "J. R. R. Tolkien", 1937}, {2, "Dune", "Frank Herbert", 1965}, {3, "Foundation", "Isaac Asimov", 1951}} {
		if _, err = db.ExecContext(c, "INSERT IGNORE INTO books (id,title,author,year) VALUES (?,?,?,?)", b.ID, b.Title, b.Author, b.Year); err != nil {
			return response{}, err
		}
	}
	rows, err := db.QueryContext(c, "SELECT id,title,author,year FROM books ORDER BY id LIMIT 100")
	if err != nil {
		return response{}, err
	}
	defer rows.Close()
	out := response{Books: []book{}, App: s.app, DatabaseTrack: t, DatabaseHost: host}
	for rows.Next() {
		var b book
		if err = rows.Scan(&b.ID, &b.Title, &b.Author, &b.Year); err != nil {
			return response{}, err
		}
		out.Books = append(out.Books, b)
	}
	return out, rows.Err()
}

var indexTemplate = template.Must(template.New("books").Parse(`<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bookshelf {{.App}} — Release Dashboard</title>
<style>
:root{font-family:system-ui,-apple-system,sans-serif;color:#12233b;background:#f3f6fb}
*{box-sizing:border-box}body{margin:0}main{max-width:1040px;margin:0 auto;padding:32px 22px 70px}
h1{margin:0;font-size:clamp(29px,4vw,42px);letter-spacing:-.04em}h2{margin:.15rem 0 .6rem;font-size:clamp(25px,3vw,35px)}
.subtitle{color:#53627b;margin:8px 0 28px}.lanes{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}
.card{border-radius:20px;padding:26px;min-width:0;box-shadow:0 14px 35px #14274612;border:1px solid #dbe2ed;background:#fff}
{{.ThemeCSS}}
.card.unknown{--accent:#626b7b;--surface:#eef0f3;--ink:#384152}
.hero{display:flex;align-items:center;gap:16px;padding:16px;border-radius:14px;background:var(--surface);color:var(--accent)}
.hero svg{width:64px;height:64px;flex:none;stroke:currentColor;fill:none;stroke-width:2.8;stroke-linecap:round;stroke-linejoin:round}
.pill{display:inline-block;font-weight:800;font-size:.8rem;letter-spacing:.06em;border-radius:100px;padding:7px 13px;background:var(--accent);color:white}
.tagline{font-size:.88rem;color:var(--ink);font-weight:650;margin-top:12px}
.role{font-weight:700;font-size:.85rem;letter-spacing:.12em;text-transform:uppercase;color:#53627b}
.identity{font-family:ui-monospace,monospace;overflow-wrap:anywhere;font-size:.8rem;color:#53627b}
.flow{font-size:24px;font-weight:800;letter-spacing:.1em;text-align:center;margin:16px;color:#72809a}
.database{margin-top:20px;background:#fff;border:1px solid #dbe2ed;border-radius:16px;padding:18px}
.database code,.help code{background:#eff2f8;padding:4px 6px;border-radius:5px;overflow-wrap:anywhere}
.tablewrap{overflow-x:auto}.books{width:100%;border-collapse:collapse;background:#fff;border-radius:14px;overflow:hidden}
.books td,.books th{text-align:left;padding:11px 16px;border-bottom:1px solid #e7ebf1}.books th{background:#e8eef9}
.help{margin-top:16px;color:#53627b;font-size:.85rem;line-height:1.8}
@media(max-width:650px){.lanes{grid-template-columns:1fr}main{padding:22px 14px}.card{padding:18px}}
</style></head><body><main>
<h1>Bookshelf {{.App}} <span aria-label="application">📚</span></h1>
<p class="subtitle">Actual running revisions, independently observed at Frontend and Backend. BLUE/YELLOW is the revision color, not a promise about current stable/canary Service ownership.</p>
<div class="lanes">
<section class="card {{.FrontendClass}}" aria-label="Frontend revision"><div class="role">Frontend</div>
<div class="hero"><svg viewBox="0 0 80 80" role="img" aria-label="Frontend monitor illustration"><rect x="7" y="12" width="66" height="43" rx="6"/><path d="M29 67h22M40 55v12"/><path d="M21 26h38M21 35h24M21 43h16"/></svg>
<div><span class="pill">{{.FrontendLabel}}</span><h2>Frontend</h2></div></div>
<p class="tagline">{{.FrontendSubtitle}}</p><p class="identity">{{.Frontend}}</p><div class="identity">Pod: {{.FrontendPod}}</div></section>
<section class="card {{.BackendClass}}" aria-label="Backend revision"><div class="role">Backend</div>
<div class="hero"><svg viewBox="0 0 80 80" role="img" aria-label="Backend server illustration"><rect x="12" y="12" width="56" height="17" rx="4"/><rect x="12" y="33" width="56" height="17" rx="4"/><rect x="12" y="54" width="56" height="17" rx="4"/><path d="M23 20h2M23 41h2M23 62h2M37 20h21M37 41h21M37 62h21"/></svg>
<div><span class="pill">{{.BackendLabel}}</span><h2>Backend</h2></div></div>
<p class="tagline">{{.BackendSubtitle}}</p><p class="identity">{{.Backend}}</p><div class="identity">Pod: {{.BackendPod}}</div></section>
</div>
<div class="flow" aria-hidden="true">↓</div>
<section class="database"><strong>MariaDB selected route:</strong> <code>{{.DatabaseTrack}}</code> via <code>{{.DatabaseHost}}</code></section>
<h2>Literature collection</h2><div class="tablewrap"><table class="books"><thead><tr><th>Title</th><th>Author</th><th>Year</th></tr></thead><tbody>
{{range .Books}}<tr><td>{{.Title}}</td><td>{{.Author}}</td><td>{{.Year}}</td></tr>{{end}}
</tbody></table></div><p class="help">Routing headers: <code>x-app-target: a|b</code>, <code>x-front-track: stable|canary</code>, <code>x-backend-track: stable|canary</code>, <code>x-db-track: blue|green</code>.<br>API evidence: <code>/api/books</code>, headers <code>X-Bookshelf-Frontend-Release</code> and <code>X-Bookshelf-Backend-Release</code>.</p>
</main></body></html>`))

func (s *server) index(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	if s.role != "front" {
		http.NotFound(w, r)
		return
	}
	b, status, err := s.upstream(r)
	if err != nil {
		http.Error(w, "backend unavailable", http.StatusBadGateway)
		return
	}
	if status != http.StatusOK {
		http.Error(w, "backend returned "+strconv.Itoa(status), http.StatusBadGateway)
		return
	}
	var out response
	if err := json.Unmarshal(b, &out); err != nil {
		http.Error(w, "invalid backend response", http.StatusBadGateway)
		return
	}
	out.Frontend = s.prefix
	out.FrontendRelease = releaseColor(s.prefix)
	out.FrontendPod = s.hostname
	palette:=s.theme()
	frontStyle:=palette.forRelease(out.FrontendRelease)
	backendStyle:=palette.forRelease(out.BackendRelease)
	out.FrontendLabel=frontStyle.Label
	out.FrontendAccent=frontStyle.Accent
	if out.BackendLabel=="" {out.BackendLabel=backendStyle.Label}
	if out.BackendAccent=="" {out.BackendAccent=backendStyle.Accent}
	setReleaseHeaders(w.Header(), out)
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	classFor := func(release string) string {
		switch release {
		case "BLUE": return "blue"
		case "YELLOW": return "yellow"
		default: return "unknown"
		}
	}
	if err := indexTemplate.Execute(w, struct {
		App, Frontend, FrontendRelease, FrontendPod, FrontendClass string
		Backend, BackendRelease, BackendPod, BackendClass string
		DatabaseTrack, DatabaseHost string
		FrontendLabel, FrontendSubtitle, BackendLabel, BackendSubtitle string
		ThemeCSS template.CSS
		Books []book
	}{
		App: s.app, Frontend: out.Frontend, FrontendRelease: out.FrontendRelease,
		FrontendPod: out.FrontendPod, FrontendClass: classFor(out.FrontendRelease),
		Backend: out.Backend, BackendRelease: out.BackendRelease,
		BackendPod: out.BackendPod, BackendClass: classFor(out.BackendRelease),
		DatabaseTrack: out.DatabaseTrack, DatabaseHost: out.DatabaseHost, Books: out.Books,
		FrontendLabel: out.FrontendLabel,FrontendSubtitle:frontStyle.Subtitle,
		BackendLabel: out.BackendLabel,BackendSubtitle:backendStyle.Subtitle,
		ThemeCSS:palette.css(),
	}); err != nil {
		log.Printf("render bookshelf dashboard: %v", err)
	}
}

func main() {
	s := newServer()
	if s.app != "A" && s.app != "B" {
		log.Fatal("APP_ID must be A or B")
	}
	if s.role != "front" && s.role != "backend" && s.role != "crud" {
		log.Fatal("ROLE must be front, backend or crud")
	}
	if s.role == "crud" {
		if s.prefix != "Application "+s.app+" - CRUD" {
			log.Fatal("invalid CRUD MESSAGE_PREFIX")
		}
	} else if s.prefix != "Application "+s.app+" - BLUE" && s.prefix != "Application "+s.app+" - YELLOW" {
		log.Fatal("invalid MESSAGE_PREFIX")
	}
	port := "3000"
	if s.role == "backend" {
		port = "5000"
	} else if s.role == "crud" {
		port = "8080"
	}
	log.Printf("bookshelf %s %s listening on %s", s.app, s.role, port)
	srv := &http.Server{Addr: ":" + port, Handler: s.routes(), ReadHeaderTimeout: 5 * time.Second}
	log.Fatal(srv.ListenAndServe())
}
