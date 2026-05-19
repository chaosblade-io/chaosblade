#!/usr/bin/env python3
"""列出当前支持的所有故障演练场景（JSON 结构化输出）"""

import json
import sys
from pathlib import Path


def main():
    catalogue_dir = Path(__file__).parent.parent / "references" / "catalogue"
    if not catalogue_dir.is_dir():
        print(f"错误: 找不到用例目录 {catalogue_dir}", file=sys.stderr)
        sys.exit(1)

    categories = []
    total = 0
    for category_dir in sorted(catalogue_dir.iterdir()):
        if not category_dir.is_dir():
            continue
        name = category_dir.name
        parts = name.split("_", 1)
        level = parts[0] if len(parts) > 1 else name
        symptom = parts[1] if len(parts) > 1 else name
        cases = sorted(
            [
                {
                    "root_cause": f.stem.split("_", 2)[-1],
                    "file": str(f.relative_to(catalogue_dir.parent.parent)),
                }
                for f in category_dir.glob("*.md")
            ],
            key=lambda x: x["root_cause"],
        )
        total += len(cases)
        categories.append({
            "category": name,
            "level": level,
            "symptom": symptom,
            "count": len(cases),
            "cases": cases,
        })

    result = {
        "total": total,
        "category_count": len(categories),
        "categories": categories,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
