package main

import (
	"os"
	"fmt"
)

const ErrPrefix = "Error:"

func printAndExitWithErrPrefix(message string) {
	fmt.Fprint(os.Stderr, fmt.Sprintf("%s %s", ErrPrefix, message))
	os.Exit(1)
}

func printErrAndExit(message string) {
	fmt.Fprint(os.Stderr, message)
	os.Exit(1)
}

func printOutputAndExit(message string) {
	fmt.Fprintf(os.Stdout, message)
	os.Exit(0)
}
