"""大 Block 处理：CSR 工程（特征工程 + 稀疏候选矩阵 + 动态贪心）"""
from typing import List, Tuple, Optional
from collections import defaultdict


def solve_large_block(
    gl_entries: List,
    bank_entries: List,
    tol: float = 0.01,
    time_window_days: int = 30,
    use_customer: bool = True,
    dynamic_greedy: bool = True
) -> Tuple[List[Tuple], List, List]:
    """
    大 Block 匹配策略（CSR 工程）

    参数:
        dynamic_greedy: True=动态贪心（候选池最小的 GL 优先处理，默认）
                       False=按 GL 原始顺序依次处理

    返回: (matches, unmatched_gl, unmatched_bank)
    matches: [(gl_entry, [bank_entries...], match_type), ...]
    """
    matches = []
    bank_used = [False] * len(bank_entries)
    bank_list = list(bank_entries)
    gl_list = list(gl_entries)

    unmatched_gl = []

    remaining_gl = list(range(len(gl_list)))

    while remaining_gl:
        if dynamic_greedy:
            # 动态贪心：每轮选候选池最小的 GL 先处理
            best_gi = None
            best_candidates = None
            best_count = float('inf')

            for gi in remaining_gl:
                candidates = _build_candidates(
                    gl_list[gi], bank_list, bank_used,
                    tol, time_window_days, use_customer
                )
                if len(candidates) < best_count:
                    best_count = len(candidates)
                    best_gi = gi
                    best_candidates = candidates

            # 修复：候选数为0时，只移除当前GL，不株连其他
            if best_gi is None:
                # 防御性判断，理论上不会发生
                break
            if best_count == 0:
                unmatched_gl.append(gl_list[best_gi])
                remaining_gl.remove(best_gi)
                continue  # 继续下一轮，其他GL还有机会
            
            g = gl_list[best_gi]
            candidates = best_candidates
        else:
            # 按 GL 原始顺序依次处理
            best_gi = remaining_gl[0]
            g = gl_list[best_gi]
            candidates = _build_candidates(
                g, bank_list, bank_used,
                tol, time_window_days, use_customer
            )
            if not candidates:
                unmatched_gl.append(g)
                remaining_gl.pop(0)
                continue

        # 在候选池内尝试漏斗策略
        match_result = _funnel_in_candidates(g, candidates, tol)

        if match_result:
            matched_banks, match_type = match_result
            matches.append((g, matched_banks, match_type))
            # 标记 Bank 为已使用
            for b in matched_banks:
                for idx, bank in enumerate(bank_list):
                    if bank is b:
                        bank_used[idx] = True
                        break
            remaining_gl.remove(best_gi)
        else:
            # 该 GL 无法匹配，放入未匹配
            unmatched_gl.append(g)
            remaining_gl.remove(best_gi)

    # 未匹配的 Bank
    unmatched_bank = [bank_list[i] for i in range(len(bank_list)) if not bank_used[i]]

    return matches, unmatched_gl, unmatched_bank


def _build_candidates(
    gl_entry,
    bank_list: List,
    bank_used: List[bool],
    tol: float,
    time_window_days: int,
    use_customer: bool
) -> List:
    """
    为单个 GL 构建 CSR 候选列表
    特征过滤：时间、金额、客商
    """
    g = gl_entry
    g_date = getattr(g, 'entry_date', None) or getattr(g, 'tx_date', None)
    g_amount = abs(g.amount) if hasattr(g, 'amount') else 0
    g_customer = getattr(g, 'customer_name', '') if use_customer else ''

    candidates = []
    for i, b in enumerate(bank_list):
        if bank_used[i]:
            continue

        b_date = getattr(b, 'tx_date', None) or getattr(b, 'entry_date', None)
        b_amount = abs(b.amount) if hasattr(b, 'amount') else float('inf')
        b_counter = getattr(b, 'counter_party', '')

        # 金额特征：Bank 金额 <= GL 金额 + tol
        if b_amount > g_amount + tol:
            continue

        # 时间特征：GL 日期 >= Bank 日期，且差距 <= time_window_days
        if g_date and b_date:
            diff = (g_date - b_date).days
            if not (0 <= diff <= time_window_days):
                continue

        # 客商特征（可选）
        if use_customer and g_customer:
            if not _fuzzy_match(g_customer, b_counter):
                continue

        candidates.append(b)

    return candidates


def _fuzzy_match(a: str, b: str, min_len: int = 2) -> bool:
    """模糊匹配：两个字符串有公共子串"""
    a = str(a).strip()
    b = str(b).strip()
    if not a or not b:
        return False
    if a == b:
        return True
    # 简单实现：互相包含或公共子串
    if a in b or b in a:
        return True
    # 更复杂的可以用 difflib.SequenceMatcher
    return False


def _funnel_in_candidates(
    gl_entry,
    candidates: List,
    tol: float
) -> Optional[Tuple[List, str]]:
    """
    在候选池内执行漏斗策略
    返回: ([bank_entries], match_type) 或 None
    """
    g_amount = abs(gl_entry.amount) if hasattr(gl_entry, 'amount') else 0

    if not candidates:
        return None

    # 1. 一对一
    for b in candidates:
        if _amount_match(g_amount, abs(b.amount), tol):
            return ([b], "csr_1v1")

    # 2. 按摘要+日期+交易方聚合（agg1 含金额，agg2~agg4 不含金额）
    for agg_level in range(1, 5):
        groups = defaultdict(list)
        for b in candidates:
            key = _agg_key_large(b, agg_level)
            groups[key].append(b)

        for grp in groups.values():
            s = sum(abs(b.amount) for b in grp)
            if _amount_match(g_amount, s, tol):
                return (grp, f"csr_agg{agg_level}")

    # 3. 子集和（限制组合大小）
    import itertools
    amounts = [abs(b.amount) for b in candidates]
    for r in range(2, min(6, len(candidates) + 1)):
        for combo in itertools.combinations(range(len(candidates)), r):
            s = sum(amounts[i] for i in combo)
            if _amount_match(g_amount, s, tol):
                return ([candidates[i] for i in combo], "csr_subset")

    return None


def _agg_key_large(bank_entry, level: int) -> tuple:
    """大 Block 聚合键"""
    b = bank_entry
    date_str = b.tx_date.strftime("%Y-%m-%d") if hasattr(b, 'tx_date') else ""
    abstract = getattr(b, 'abstract', '')
    counter_party = getattr(b, 'counter_party', '')
    amount = round(b.amount, 2)

    if level == 1:
        return (abstract, date_str, counter_party, amount)
    elif level == 2:
        return (abstract, date_str, counter_party)
    elif level == 3:
        return (date_str, counter_party)
    else:
        return (counter_party,)


def _amount_match(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol
