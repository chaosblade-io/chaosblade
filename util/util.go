package util

import (
	"path"
	"math/rand"
	"encoding/hex"
	"time"
	"os"
	"log"
	"reflect"
	"os/user"
	"net"
	"net/http"
	"io/ioutil"
	"context"
	"os/exec"
	"path/filepath"
	"fmt"
)

var proPath string
var binPath string
var libPath string

// GetProgramPath
func GetProgramPath() string {
	if proPath != "" {
		return proPath
	}
	dir, err := exec.LookPath(os.Args[0])
	if err != nil {
		log.Fatal("can get the process path")
	}
	if p, err := os.Readlink(dir); err == nil {
		dir = p
	}
	proPath, err = filepath.Abs(filepath.Dir(dir))
	if err != nil {
		log.Fatal("can get the full process path")
	}
	return proPath
}

// GetBinPath
func GetBinPath() string {
	if binPath != "" {
		return binPath
	}
	binPath = path.Join(GetProgramPath(), "bin")
	return binPath
}

// GetLibHome
func GetLibHome() string {
	if libPath != "" {
		return libPath
	}
	libPath = path.Join(GetProgramPath(), "lib")
	return libPath
}

// GenerateUid for exp
func GenerateUid() (string, error) {
	rand.Seed(time.Now().UnixNano())
	b := make([]byte, 8)
	_, err := rand.Read(b)
	if err != nil {
		return "", err
	}
	return hex.EncodeToString(b), nil
}

func IsNil(i interface{}) bool {
	v := reflect.ValueOf(i)
	if v.Kind() == reflect.Ptr {
		return v.IsNil()
	}
	return false
}

//IsExist returns true if file exists
func IsExist(fileName string) bool {
	_, err := os.Stat(fileName)
	return err == nil || os.IsExist(err)
}

// IsDir returns true if the path is directory
func IsDir(path string) bool {
	fileInfo, err := os.Stat(path)
	if err != nil || fileInfo == nil {
		return false
	}
	return fileInfo.IsDir()
}

//GetUserHome return user home.
func GetUserHome() string {
	user, err := user.Current()
	if err == nil {
		return user.HomeDir
	}
	return "/root"
}

// GetSpecifyingUserHome
func GetSpecifyingUserHome(username string) string {
	usr, err := user.Lookup(username)
	if err == nil {
		return usr.HomeDir
	}
	return fmt.Sprintf("/home/%s", username)
}

// Curl url
func Curl(url string) (string, error, int) {
	trans := http.Transport{
		DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			return net.DialTimeout(network, addr, 10*time.Second)
		},
	}
	client := http.Client{
		Transport: &trans,
	}
	resp, err := client.Get(url)
	if err != nil {
		return "", err, 0
	}
	defer resp.Body.Close()
	bytes, err := ioutil.ReadAll(resp.Body)
	if err != nil {
		return "", err, resp.StatusCode
	}
	return string(bytes), nil, resp.StatusCode
}

// CheckPortInUse returns true if the port is in use, otherwise returns false.
func CheckPortInUse(port string) bool {
	conn, err := net.DialTimeout("tcp", net.JoinHostPort("127.0.0.1", port), time.Second)
	if err != nil {
		return false
	}
	defer conn.Close()
	if conn != nil {
		return true
	}
	return false
}

func GetUnusedPort() (int, error) {
	addr, err := net.ResolveTCPAddr("tcp", "127.0.0.1:0")
	if err != nil {
		return 0, err
	}
	listener, err := net.ListenTCP("tcp", addr)
	if err != nil {
		return 0, err
	}
	defer listener.Close()
	return listener.Addr().(*net.TCPAddr).Port, nil
}

// GetProgramParentPath returns the parent directory end with /
func GetProgramParentPath() string {
	dir, _ := path.Split(GetProgramPath())
	return dir
}
