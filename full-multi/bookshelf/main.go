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
}
type server struct {
	role, app, prefix, backendURL, dbStable, dbCanary, dbName, dbUser, dbPassword string
	client                                                                        *http.Client
}

func track(h http.Header, name string) string {
	if strings.EqualFold(strings.TrimSpace(h.Get(name)), "canary") {
		return "canary"
	}
	return "stable"
}
func newServer() *server {
	return &server{role: os.Getenv("ROLE"), app: os.Getenv("APP_ID"), prefix: os.Getenv("MESSAGE_PREFIX"), backendURL: os.Getenv("BACKEND_API_URL"), dbStable: os.Getenv("DB_HOST_STABLE"), dbCanary: os.Getenv("DB_HOST_CANARY"), dbName: os.Getenv("DB_NAME"), dbUser: os.Getenv("DB_USER"), dbPassword: os.Getenv("DB_PASSWORD"), client: &http.Client{Timeout: 8 * time.Second}}
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
	u, err := url.Parse(s.backendURL)
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
	if s.role == "front" {
		b, status, err := s.upstream(r)
		if err != nil {
			http.Error(w, "backend unreachable: "+err.Error(), 502)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write(b)
		return
	}
	if s.role != "backend" {
		http.Error(w, "invalid role", 500)
		return
	}
	out, err := s.queryBooks(r.Context(), track(r.Header, "x-db-track"))
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
	out := response{Books: []book{}, App: s.app, DatabaseTrack: t, DatabaseHost: host, Backend: s.prefix}
	for rows.Next() {
		var b book
		if err = rows.Scan(&b.ID, &b.Title, &b.Author, &b.Year); err != nil {
			return response{}, err
		}
		out.Books = append(out.Books, b)
	}
	return out, rows.Err()
}

var indexTemplate = template.Must(template.New("books").Parse(`<!doctype html><html><head><title>Full Multi Bookshelf</title><style>body{font-family:system-ui;margin:2.5rem;max-width:900px}table{border-collapse:collapse}td,th{padding:.6rem;border-bottom:1px solid #bbb}code{background:#eee;padding:2px}</style></head><body><h1>Bookshelf {{.App}}</h1><p>{{.Prefix}}</p><p>Database track: <code>{{.Track}}</code> via <code>{{.Host}}</code></p><table><tr><th>Title</th><th>Author</th><th>Year</th></tr>{{range .Books}}<tr><td>{{.Title}}</td><td>{{.Author}}</td><td>{{.Year}}</td></tr>{{end}}</table><p>Headers: x-app-target=a|b, x-front-track=stable|canary, x-backend-track=stable|canary, x-db-track=stable|canary.</p></body></html>`))

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
		http.Error(w, "backend unavailable", 502)
		return
	}
	if status != 200 {
		http.Error(w, "backend returned "+strconv.Itoa(status), 502)
		return
	}
	var out response
	if err = json.Unmarshal(b, &out); err != nil {
		http.Error(w, "invalid backend response", 502)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_ = indexTemplate.Execute(w, struct {
		App, Prefix, Track, Host string
		Books                    []book
	}{s.app, s.prefix, out.DatabaseTrack, out.DatabaseHost, out.Books})
}
func main() {
	s := newServer()
	if s.app != "A" && s.app != "B" {
		log.Fatal("APP_ID must be A or B")
	}
	if s.role != "front" && s.role != "backend" {
		log.Fatal("ROLE must be front or backend")
	}
	if s.prefix != "Application "+s.app+" - BLUE" && s.prefix != "Application "+s.app+" - YELLOW" {
		log.Fatal("invalid MESSAGE_PREFIX")
	}
	port := "3000"
	if s.role == "backend" {
		port = "5000"
	}
	log.Printf("bookshelf %s %s listening on %s", s.app, s.role, port)
	srv := &http.Server{Addr: ":" + port, Handler: s.routes(), ReadHeaderTimeout: 5 * time.Second}
	log.Fatal(srv.ListenAndServe())
}
