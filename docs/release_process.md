# ChaosBlade 版本发布流程

本文档描述了 ChaosBlade 项目的版本发布流程，包括从 Git Tag 到自动化构建再到编译产物包含版本的完整流程。

## 📋 目录

- [版本发布概述](#版本发布概述)
- [版本号规范](#版本号规范)
- [自动化发布流程](#自动化发布流程)
- [手动发布流程](#手动发布流程)
- [版本信息验证](#版本信息验证)
- [常见问题](#常见问题)

## 🎯 版本发布概述

ChaosBlade 采用语义化版本控制（Semantic Versioning），版本号格式为 `X.Y.Z[-prerelease][+build]`。

### 版本类型

- **主版本号 (X)**: 不兼容的 API 修改
- **次版本号 (Y)**: 向下兼容的功能性新增
- **修订版本号 (Z)**: 向下兼容的问题修正
- **预发布版本**: 如 `1.8.0-beta.1`, `1.8.0-rc.1`
- **构建版本**: 如 `1.8.0+20231201`

## 🏷️ 版本号规范

### 版本号格式

```
X.Y.Z[-prerelease][+build]
```

### 示例

- `1.8.0` - 正式发布版本
- `1.8.0-beta.1` - Beta 预发布版本
- `1.8.0-rc.1` - Release Candidate 版本
- `1.8.0+20231201` - 带构建信息的版本

### 版本号规则

1. **主版本号**: 重大变更，不兼容旧版本
2. **次版本号**: 新功能，兼容旧版本
3. **修订版本号**: Bug 修复，兼容旧版本
4. **预发布版本**: 开发阶段版本，不稳定
5. **构建版本**: 构建元数据，不影响版本比较

## 🤖 自动化发布流程

### 触发条件

当推送 Git Tag 到远程仓库时，GitHub Actions 会自动触发发布流程：

```bash
# 推送标签触发自动化构建
git tag v1.8.0
git push origin v1.8.0
```

### 自动化流程步骤

1. **版本检查**: 验证版本号格式和 Git 状态
2. **版本信息生成**: 自动生成版本信息文件
3. **多平台构建**: 构建所有支持平台的二进制文件
4. **版本验证**: 验证编译产物中的版本信息
5. **创建 Release**: 自动创建 GitHub Release
6. **上传产物**: 上传所有构建产物到 Release

### 支持的平台

- **Linux**: AMD64, ARM64
- **macOS**: AMD64 (Intel), ARM64 (Apple Silicon)
- **Windows**: AMD64

## 🛠️ 手动发布流程

### 使用发布脚本

我们提供了自动化发布脚本 `scripts/release.sh`：

```bash
# 完整发布流程
./scripts/release.sh 1.8.0

# 仅构建
./scripts/release.sh -b 1.8.0

# 仅创建标签
./scripts/release.sh -t 1.8.0

# 仅创建 Release
./scripts/release.sh -r 1.8.0

# 试运行模式
./scripts/release.sh -d 1.8.0

# 强制发布（跳过检查）
./scripts/release.sh -f 1.8.0
```

### 手动发布步骤

1. **准备发布**
   ```bash
   # 确保在 master/main 分支
   git checkout master
   
   # 确保工作目录干净
   git status
   
   # 拉取最新代码
   git pull origin master
   ```

2. **创建标签**
   ```bash
   # 创建带注释的标签
   git tag -a v1.8.0 -m "Release v1.8.0"
   
   # 推送标签
   git push origin v1.8.0
   ```

3. **等待自动化构建**
   - GitHub Actions 会自动构建所有平台
   - 构建完成后自动创建 Release
   - 所有产物自动上传到 Release

## 🔍 版本信息验证

### 检查版本信息

```bash
# 查看版本信息
./blade version

# 输出示例
ChaosBlade Version Information:
==============================
Version:     1.8.0
Git Tag:     v1.8.0
Git Commit:  a1b2c3d
Git Branch:  master
Build Time:  2023-12-01 10:00:00 UTC
Environment: darwin amd64
Release:     Yes (Production)
==============================
```

### 验证构建产物

```bash
# 解压构建产物
tar -xzf chaosblade-1.8.0-linux_amd64.tar.gz
cd chaosblade-1.8.0-linux_amd64

# 验证版本
./blade version

# 验证文件完整性
ls -la
```

## 📁 文件结构

```
chaosblade/
├── scripts/
│   ├── version.sh          # 版本信息生成脚本
│   └── release.sh          # 发布脚本
├── version/
│   ├── version.go          # 版本包基础定义
│   └── version_info.go     # 自动生成的版本信息
├── .github/
│   └── workflows/
│       ├── ci.yml          # CI 工作流
│       └── release.yml     # 发布工作流
└── Makefile                # 构建配置
```

## 🔧 配置说明

### Makefile 配置

Makefile 已更新为支持自动版本检测：

```makefile
# 版本信息管理
# 支持从Git Tag自动获取版本号
GIT_TAG := $(shell git describe --tags --abbrev=0 2>/dev/null || echo "")
ifeq ($(GIT_TAG),)
    # 如果没有Git Tag，使用默认版本或环境变量
    BLADE_VERSION ?= 1.7.4
else
    # 从Git Tag提取版本号（移除v前缀）
    BLADE_VERSION := $(shell echo $(GIT_TAG) | sed 's/^v//')
endif
```

### GitHub Actions 配置

发布工作流配置在 `.github/workflows/release.yml` 中，包括：

- 版本检查和验证
- 多平台构建
- 版本信息验证
- 自动创建 Release
- 产物上传

## ❓ 常见问题

### Q: 如何回滚版本？

A: 不建议删除已发布的标签。如果需要修复问题，建议发布新的修订版本。

### Q: 预发布版本如何处理？

A: 预发布版本（如 beta、rc）会触发相同的构建流程，但不会自动创建正式 Release。

### Q: 构建失败怎么办？

A: 检查 GitHub Actions 日志，修复问题后重新推送标签。

### Q: 如何自定义 Release 说明？

A: 可以手动编辑 GitHub Release 页面，或修改 `scripts/release.sh` 中的模板。

### Q: 支持哪些版本号格式？

A: 支持标准的语义化版本格式，包括预发布和构建元数据。

## 📞 支持

如果在发布过程中遇到问题，请：

1. 查看 GitHub Actions 日志
2. 检查版本号格式是否正确
3. 确保 Git 状态正常
4. 在 GitHub Issues 中报告问题

## 📚 相关链接

- [语义化版本控制](https://semver.org/)
- [GitHub Actions 文档](https://docs.github.com/en/actions)
- [GitHub CLI 文档](https://cli.github.com/)
- [ChaosBlade 项目主页](https://github.com/chaosblade-io/chaosblade)
