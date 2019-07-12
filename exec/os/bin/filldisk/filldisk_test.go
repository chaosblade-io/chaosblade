package main

import (
	"testing"
	"os"
	"path"
	"strconv"
)

func TestStartAndStopFill(t *testing.T) {
	t.Skip("")
	inputs := []struct{
		mountPoint, count string
	}{
		{"/dev", "1"},
		{"/dev", "4"},
		{"/dev", "16"},
	}

	for _, it := range inputs {
		startFill(it.mountPoint, it.count)
		dataFile := path.Join(it.mountPoint, fillDataFile)
		ct, _ := strconv.Atoi(it.count)
		expect := getExpectedFileSize(1000000, ct)
		got := getRealFileSize(dataFile)
		if expect != got {
			t.Errorf("unexpected result: %d, expected: %d", got, expect)
		}
		stopFill(it.mountPoint)
	}
}

func getExpectedFileSize(size, count int) int64 {
	return int64(size * count)
}

func getRealFileSize(file string) int64 {
	fileInfo, _ := os.Stat(file)
	filesize := fileInfo.Size()
	return filesize
}