#!/usr/bin/env python3
"""列出 host-chaos-skills 支持的所有主机故障演练场景（JSON 结构化输出）"""

import json
import os
from pathlib import Path


def list_scenarios():
    """扫描 catalogue 目录，输出所有故障场景的结构化信息"""
    catalogue_dir = Path(__file__).parent.parent / "references" / "catalogue"

    if not catalogue_dir.exists():
        print(json.dumps({"error": f"catalogue directory not found: {catalogue_dir}"}, ensure_ascii=False))
        return

    scenarios = []
    for category_dir in sorted(catalogue_dir.iterdir()):
        if not category_dir.is_dir():
            continue

        category_name = category_dir.name
        cases = []
        for case_file in sorted(category_dir.glob("*.md")):
            cases.append({
                "file": case_file.name,
                "root_cause": case_file.stem.split("_", 2)[-1] if "_" in case_file.stem else case_file.stem,
            })

        scenarios.append({
            "category": category_name,
            "case_count": len(cases),
            "cases": cases,
        })

    output = {
        "skill": "host-chaos-skills",
        "scope": "主机级（物理机/虚拟机/ECS）",
        "total_categories": len(scenarios),
        "total_cases": sum(s["case_count"] for s in scenarios),
        "scenarios": scenarios,
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    list_scenarios()
