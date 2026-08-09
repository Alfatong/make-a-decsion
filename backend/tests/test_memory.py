"""记忆层生产代码单元测试（不依赖外部 API）"""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.services.memory.fact_store import FactStore
from app.services.memory.checker import RuleChecker


def test_fact_trusted_isolation():
    path = tempfile.mktemp(suffix=".db")
    fs = FactStore(path, "book1")
    fs.add("item", "借条", "state", "已归还", 2, source="outline")
    fs.add("item", "借条", "state", "被烧毁", 3, source="model")  # 模型误抽
    # 高可信只看 outline
    v, ch = fs.latest("item", "借条", "state", trusted_only=True)
    assert v == "已归还", f"可信事实被模型污染: {v}"
    # 全量看则最新是 model
    v2, _ = fs.latest("item", "借条", "state", trusted_only=False)
    assert v2 == "被烧毁"
    fs.close(); os.remove(path)
    print("✓ 事实来源隔离（预置不被模型污染）")


def test_rule_checker():
    path = tempfile.mktemp(suffix=".db")
    fs = FactStore(path, "book1")
    fs.add("character", "刘婶", "alive", "去世", 17, source="outline")
    fs.add("item", "借条", "state", "已归还", 2, source="outline")
    fs.add("character", "李长顺", "location", "搬到向阳南屋", 18, source="outline")
    ck = RuleChecker()
    r1 = ck.check("刘婶端着饺子走进来，笑着说：'趁热吃。'", fs)
    assert any(c["type"] == "角色复活" for c in r1), "角色复活未拦截"
    r2 = ck.check("赵铁柱把借条又拿出来说事。", fs)
    assert any(c["type"] == "道具复现" for c in r2), "道具复现未拦截"
    r3 = ck.check("李长顺回到西厢房糊窗缝。", fs)
    assert any(c["type"] == "住处矛盾" for c in r3), "住处矛盾未拦截"
    r4 = ck.check("李长顺在南屋看了看新奖状。", fs)
    assert not r4, f"误报: {r4}"
    fs.close(); os.remove(path)
    print("✓ 规则校验器（角色复活/道具复现/住处矛盾/无误报）")


if __name__ == "__main__":
    test_fact_trusted_isolation()
    test_rule_checker()
    print("\n全部通过")
