package version

var Version = &version{
	Ver:       "unknown",
	Env:       "oss",
	BuildTime: "unknown",
}

type version struct {
	Ver       string
	Env       string
	BuildTime string
}

func InitVersion(ver, env, buildTime string) {
	Version.Ver = ver
	Version.Env = env
	Version.BuildTime = buildTime
}
