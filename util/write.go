package util

import (
	"os"
	"io"
)

// FileWriterWithoutErr write func does not return err under any conditions
// To solve "Failed to write to log, write logs/chaosblade.log: no space left on device" err
type FileWriterWithoutErr struct {
	*os.File
}

func NewFileWriterWithoutErr(file *os.File) io.Writer {
	return &FileWriterWithoutErr{file}
}

func (f *FileWriterWithoutErr) Write(b []byte) (n int, err error) {
	i, _ := f.File.Write(b)
	return i, nil
}
