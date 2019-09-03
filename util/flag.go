package util

import (
	"strings"
	"strconv"
	"fmt"
)

// ParseIntegerListToStringSlice func parses the multiple integer values to string slice.
// Support the below formats: 0 | 0,1 | 0,2,3 | 0-3 | 0,2-4 | 0,1,3-5
// For example, the flag value is 0,2-3, the func returns []string{"0", "2", "3"}
func ParseIntegerListToStringSlice(flagValue string) ([]string, error) {
	values := make([]string, 0)
	commaParts := strings.Split(flagValue, ",")
	for _, part := range commaParts {
		value := strings.TrimSpace(part)
		if value == "" {
			continue
		}
		if !strings.Contains(value, "-") {
			_, err := strconv.Atoi(value)
			if err != nil {
				return values, fmt.Errorf("%s value is illegal, %v", value, err)
			}
			values = append(values, value)
			continue
		}
		ranges := strings.Split(value, "-")
		if len(ranges) != 2 {
			return values, fmt.Errorf("%s value is illegal", value)
		}
		startIndex, err := strconv.Atoi(strings.TrimSpace(ranges[0]))
		if err != nil {
			return values, fmt.Errorf("start in %s value is illegal", value)
		}
		endIndex, err := strconv.Atoi(strings.TrimSpace(ranges[1]))
		if err != nil {
			return values, fmt.Errorf("end in %s value is illegal", value)
		}
		for i := startIndex; i <= endIndex; i++ {
			values = append(values, strconv.Itoa(i))
		}
	}
	return values, nil
}
