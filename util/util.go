package util

import (
	"path"
	"math/rand"
	"encoding/hex"
	"time"
	"path/filepath"
	"os"
	"log"
	"reflect"
	"os/user"
	"net"
	"net/http"
	"io/ioutil"
	"context"
)

var proPath string
var binPath string
var libPath string

// GetProgramPath
func GetProgramPath() string {
	if proPath != "" {
		return proPath
	}
	dir, err := filepath.Abs(filepath.Dir(os.Args[0]))
	if err != nil {
		log.Fatal("can get the process path")
	}
	proPath = dir
	return dir
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

//IsExist return true if file exists
func IsExist(fileName string) bool {
	_, err := os.Stat(fileName)
	return err == nil || os.IsExist(err)
}

//GetUserHome return user home.
func GetUserHome() string {
	user, err := user.Current()
	if err == nil {
		return user.HomeDir
	}
	return "/root"
}

// Curl url
func Curl(url string) (string, error) {
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
		return "", err
	}
	defer resp.Body.Close()
	bytes, err := ioutil.ReadAll(resp.Body)
	if err != nil {
		return "", err
	}
	return string(bytes), nil
}
