package util

import (
	"reflect"
	"testing"
)

func TestRemove(t *testing.T) {
	type args struct {
		items []string
		idx   int
	}
	tests := []struct {
		name string
		args args
		want []string
	}{
		{
			args: struct {
				items []string
				idx   int
			}{items: []string{"1", "2", "3"}, idx: 2},
			want: []string{"1", "2"},
		},
		{
			args: struct {
				items []string
				idx   int
			}{items: []string{"1", "2", "3"}, idx: 0},
			want: []string{"3", "2"},
		},
		{
			args: struct {
				items []string
				idx   int
			}{items: []string{"1", "2", "3"}, idx: 1},
			want: []string{"1", "3"},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := Remove(tt.args.items, tt.args.idx); !reflect.DeepEqual(got, tt.want) {
				t.Errorf("Remove() = %v, want %v", got, tt.want)
			}
		})
	}
}
