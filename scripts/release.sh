#!/bin/bash
# Copyright 2025 The ChaosBlade Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# ChaosBlade 版本发布脚本
# 用于自动化版本发布流程

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 打印带颜色的消息
print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 显示帮助信息
show_help() {
    cat << EOF
ChaosBlade 版本发布脚本

用法:
    $0 [选项] <版本号>

选项:
    -h, --help          显示此帮助信息
    -d, --dry-run       试运行模式（不实际执行）
    -f, --force         强制发布（跳过检查）
    -p, --pre-release   预发布版本
    -b, --build         构建发布包
    -t, --tag           创建Git标签
    -r, --release       创建GitHub Release

参数:
    版本号              版本号格式: X.Y.Z[-prerelease][+build]
                       例如: 1.8.0, 1.8.0-beta.1, 1.8.0+20231201

示例:
    $0 1.8.0                    # 发布版本 1.8.0
    $0 -b 1.8.0                # 仅构建版本 1.8.0
    $0 -t 1.8.0                # 仅创建标签
    $0 -r 1.8.0                # 仅创建Release
    $0 -d 1.8.0                # 试运行模式

注意事项:
    1. 确保当前分支是 master 或 main
    2. 确保工作目录干净（无未提交的更改）
    3. 确保有推送权限
    4. 版本号必须符合语义化版本规范
EOF
}

# 验证版本号格式
validate_version() {
    local version=$1
    if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[a-zA-Z0-9.-]+)?(\+[a-zA-Z0-9.-]+)?$ ]]; then
        print_error "无效的版本号格式: $version"
        print_error "期望格式: X.Y.Z[-prerelease][+build]"
        exit 1
    fi
    print_success "版本号格式验证通过: $version"
}

# 检查Git状态
check_git_status() {
    print_info "检查Git状态..."
    
    # 检查是否在Git仓库中
    if ! git rev-parse --git-dir > /dev/null 2>&1; then
        print_error "当前目录不是Git仓库"
        exit 1
    fi
    
    # 检查当前分支
    local current_branch=$(git rev-parse --abbrev-ref HEAD)
    if [[ "$current_branch" != "master" && "$current_branch" != "main" ]]; then
        print_warning "当前分支不是 master 或 main: $current_branch"
        if [[ "$FORCE" != "true" ]]; then
            print_error "请切换到 master 或 main 分支，或使用 --force 选项"
            exit 1
        fi
    fi
    
    # 检查工作目录是否干净
    if ! git diff-index --quiet HEAD --; then
        print_error "工作目录不干净，有未提交的更改"
        git status --short
        exit 1
    fi
    
    # 检查是否有未推送的提交
    local ahead=$(git rev-list --count origin/$current_branch..HEAD)
    if [[ $ahead -gt 0 ]]; then
        print_warning "有 $ahead 个未推送的提交"
        if [[ "$FORCE" != "true" ]]; then
            print_error "请先推送所有提交，或使用 --force 选项"
            exit 1
        fi
    fi
    
    print_success "Git状态检查通过"
}

# 更新版本信息
update_version() {
    local version=$1
    print_info "更新版本信息到 $version..."
    
    # 生成版本信息
    if [[ -f "scripts/version.sh" ]]; then
        chmod +x scripts/version.sh
        ./scripts/version.sh
        print_success "版本信息已更新"
    else
        print_warning "未找到 scripts/version.sh，跳过版本信息更新"
    fi
}

# 构建项目
build_project() {
    local version=$1
    print_info "构建项目版本 $version..."
    
    if [[ "$DRY_RUN" == "true" ]]; then
        print_info "[DRY-RUN] 执行: make build_all"
        return 0
    fi
    
    # 清理之前的构建
    make clean
    
    # 构建所有组件
    make build_all
    
    print_success "项目构建完成"
}

# 创建Git标签
create_git_tag() {
    local version=$1
    local tag_name="v$version"
    
    print_info "创建Git标签: $tag_name"
    
    if [[ "$DRY_RUN" == "true" ]]; then
        print_info "[DRY-RUN] 执行: git tag -a $tag_name -m \"Release $tag_name\""
        print_info "[DRY-RUN] 执行: git push origin $tag_name"
        return 0
    fi
    
    # 检查标签是否已存在
    if git tag -l | grep -q "^$tag_name$"; then
        print_warning "标签 $tag_name 已存在"
        if [[ "$FORCE" != "true" ]]; then
            print_error "请删除现有标签或使用 --force 选项"
            exit 1
        fi
        print_info "删除现有标签..."
        git tag -d "$tag_name"
        git push origin ":refs/tags/$tag_name" 2>/dev/null || true
    fi
    
    # 创建带注释的标签
    git tag -a "$tag_name" -m "Release $tag_name"
    
    # 推送标签
    git push origin "$tag_name"
    
    print_success "Git标签 $tag_name 已创建并推送"
}

# 创建GitHub Release
create_github_release() {
    local version=$1
    local tag_name="v$version"
    
    print_info "创建GitHub Release: $tag_name"
    
    if [[ "$DRY_RUN" == "true" ]]; then
        print_info "[DRY-RUN] 使用 GitHub CLI 创建 Release"
        return 0
    fi
    
    # 检查是否安装了 GitHub CLI
    if ! command -v gh >/dev/null 2>&1; then
        print_error "未安装 GitHub CLI (gh)，请先安装"
        print_info "安装方法: https://cli.github.com/"
        exit 1
    fi
    
    # 检查是否已登录
    if ! gh auth status >/dev/null 2>&1; then
        print_error "GitHub CLI 未登录，请先登录"
        print_info "执行: gh auth login"
        exit 1
    fi
    
    # 创建Release
    local release_body="## ChaosBlade $version

### 🚀 What's New
This release includes various improvements and bug fixes.

### 📦 Downloads
Choose the appropriate package for your platform:

**CLI Only:**
- **Linux AMD64**: For 64-bit Linux systems
- **Linux ARM64**: For ARM64 Linux systems  
- **Darwin AMD64**: For Intel-based macOS
- **Darwin ARM64**: For Apple Silicon macOS
- **Windows AMD64**: For 64-bit Windows systems

**Full Package (Linux AMD64):**
- Includes all components: CLI, OS, Cloud, Middleware, JVM, CRI, Kubernetes, etc.

### 🔧 Installation
\`\`\`bash
# Extract and install
tar -xzf chaosblade-$version-[platform].tar.gz
cd chaosblade-$version-[platform]
sudo cp blade /usr/local/bin/

# Verify installation
blade version
\`\`\`

### 📋 Changes
See [CHANGELOG](CHANGELOG.md) for detailed changes.

### 🐛 Bug Reports
If you encounter any issues, please report them on [GitHub Issues](https://github.com/chaosblade-io/chaosblade/issues)."
    
    gh release create "$tag_name" \
        --title "ChaosBlade $version" \
        --notes "$release_body" \
        --draft false \
        --prerelease false
    
    print_success "GitHub Release $tag_name 已创建"
}

# 主函数
main() {
    local version=""
    local do_build=false
    local do_tag=false
    local do_release=false
    
    # 解析命令行参数
    while [[ $# -gt 0 ]]; do
        case $1 in
            -h|--help)
                show_help
                exit 0
                ;;
            -d|--dry-run)
                DRY_RUN=true
                shift
                ;;
            -f|--force)
                FORCE=true
                shift
                ;;
            -p|--pre-release)
                PRE_RELEASE=true
                shift
                ;;
            -b|--build)
                do_build=true
                shift
                ;;
            -t|--tag)
                do_tag=true
                shift
                ;;
            -r|--release)
                do_release=true
                shift
                ;;
            -*)
                print_error "未知选项: $1"
                show_help
                exit 1
                ;;
            *)
                if [[ -z "$version" ]]; then
                    version=$1
                else
                    print_error "只能指定一个版本号"
                    exit 1
                fi
                shift
                ;;
        esac
    done
    
    # 检查版本号参数
    if [[ -z "$version" ]]; then
        print_error "请指定版本号"
        show_help
        exit 1
    fi
    
    # 如果没有指定具体操作，执行完整流程
    if [[ "$do_build" == "false" && "$do_tag" == "false" && "$do_release" == "false" ]]; then
        do_build=true
        do_tag=true
        do_release=true
    fi
    
    print_info "开始发布流程..."
    print_info "版本号: $version"
    print_info "试运行模式: ${DRY_RUN:-false}"
    print_info "强制模式: ${FORCE:-false}"
    print_info "预发布: ${PRE_RELEASE:-false}"
    
    # 验证版本号格式
    validate_version "$version"
    
    # 检查Git状态
    check_git_status
    
    # 更新版本信息
    update_version "$version"
    
    # 构建项目
    if [[ "$do_build" == "true" ]]; then
        build_project "$version"
    fi
    
    # 创建Git标签
    if [[ "$do_tag" == "true" ]]; then
        create_git_tag "$version"
    fi
    
    # 创建GitHub Release
    if [[ "$do_release" == "true" ]]; then
        create_github_release "$version"
    fi
    
    print_success "版本 $version 发布流程完成！"
    
    if [[ "$do_tag" == "true" ]]; then
        print_info "GitHub Actions 将自动构建并上传发布包"
        print_info "请查看: https://github.com/$(git config --get remote.origin.url | sed 's/.*github.com[:/]\([^/]*\/[^/]*\).*/\1/')/actions"
    fi
}

# 设置默认值
DRY_RUN=${DRY_RUN:-false}
FORCE=${FORCE:-false}
PRE_RELEASE=${PRE_RELEASE:-false}

# 执行主函数
main "$@"
