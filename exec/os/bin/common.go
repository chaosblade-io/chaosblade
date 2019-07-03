package bin

import (
	"fmt"
	"os"
)

const ErrPrefix = "Error:"

func PrintAndExitWithErrPrefix(message string) {
	fmt.Fprint(os.Stderr, fmt.Sprintf("%s %s", ErrPrefix, message))
	os.Exit(1)
}

func PrintErrAndExit(message string) {
	fmt.Fprint(os.Stderr, message)
	os.Exit(1)
}

func PrintOutputAndExit(message string) {
	fmt.Fprintf(os.Stdout, message)
	os.Exit(0)
}
