import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".tools/python"))
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HOME"] = str(ROOT / ".tools/huggingface")
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["PYTHONUTF8"] = "1"
from laobu.cli import main

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已中断，完整阶段检查点已保留。下次以同一配置运行process续跑。", file=sys.stderr)
        raise SystemExit(130)
    except Exception as e:
        print(f"错误：{type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(1)
