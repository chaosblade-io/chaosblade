package util

// Remove the item by the index
func Remove(items []string, idx int) []string {
	length := len(items)
	items[length-1], items[idx] = items[idx], items[length-1]
	return items[:length-1]
}
