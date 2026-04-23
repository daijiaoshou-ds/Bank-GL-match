"""小 Block 处理：漏斗策略 + 规划求解"""
import itertools
from typing import List, Tuple, Optional, Dict, Set
from collections import defaultdict


def solve_small_block(
    gl_entries: List,
    bank_entries: List,
    tol: float = 0.01,
    date_window_days: int = 30
) -> Tuple[List[Tuple], List, List]:
    """
    小 Block 匹配策略（漏斗策略）
    
    返回: (matches, unmatched_gl, unmatched_bank)
    matches: [(gl_entry, [bank_entries...], match_type), ...]
    """
    matches = []
    unmatched_gl = list(gl_entries)
    unmatched_bank = list(bank_entries)

    # 步骤0: 一对一精确匹配
    _match_one_to_one(unmatched_gl, unmatched_bank, matches, tol, date_window_days)

    # 步骤1-4: 四级聚合漏斗
    for agg_level in range(1, 5):
        if not unmatched_bank or not unmatched_gl:
            break
        _match_by_aggregation(unmatched_gl, unmatched_bank, matches, agg_level, tol, date_window_days)

    # 步骤5: DP 子集和凑数
    if unmatched_gl and unmatched_bank:
        _match_subset_sum_dp(unmatched_gl, unmatched_bank, matches, tol, date_window_days)

    return matches, unmatched_gl, unmatched_bank


def _match_one_to_one(
    gl_list: List,
    bank_list: List,
    matches: List,
    tol: float,
    date_window_days: int = 30
):
    """一对一精确匹配：金额相等 + 日期接近"""
    to_remove_gl = []
    to_remove_bank = set()

    for gi, g in enumerate(gl_list):
        for bi, b in enumerate(bank_list):
            if bi in to_remove_bank:
                continue
            if _amount_match(g.amount, b.amount, tol) and _date_close(g, b, date_window_days):
                matches.append((g, [b], "1v1"))
                to_remove_gl.append(gi)
                to_remove_bank.add(bi)
                break

    # 移除已匹配
    for gi in sorted(to_remove_gl, reverse=True):
        gl_list.pop(gi)

    for bi in sorted(to_remove_bank, reverse=True):
        bank_list.pop(bi)


def _match_by_aggregation(
    gl_list: List,
    bank_list: List,
    matches: List,
    agg_level: int,
    tol: float,
    date_window_days: int = 30
):
    """
    按聚合级别匹配 Bank 条目
    agg_level:
      1: 摘要+日期+交易方+金额
      2: 摘要+日期+交易方
      3: 日期+交易方
      4: 交易方
    """
    # 先按聚合键对 Bank 分组并求和
    groups = defaultdict(list)
    for b in bank_list:
        key = _agg_key(b, agg_level)
        groups[key].append(b)

    # 计算每组的累计金额
    group_sums = {key: sum(b.amount for b in grp) for key, grp in groups.items()}

    to_remove_gl = []
    used_groups = set()

    for gi, g in enumerate(gl_list):
        for key, grp in groups.items():
            if key in used_groups:
                continue
            if _amount_match(g.amount, group_sums[key], tol) and _date_close_group(g, grp, date_window_days):
                matches.append((g, grp, f"agg{agg_level}"))
                to_remove_gl.append(gi)
                used_groups.add(key)
                break

    # 移除已匹配的 GL
    for gi in sorted(to_remove_gl, reverse=True):
        gl_list.pop(gi)

    # 移除已匹配的 Bank（整个组移除）
    to_remove_bank = set()
    for key in used_groups:
        for b in groups[key]:
            for i, existing in enumerate(bank_list):
                if existing is b:
                    to_remove_bank.add(i)
                    break

    for bi in sorted(to_remove_bank, reverse=True):
        bank_list.pop(bi)


def _match_subset_sum_dp(
    gl_list: List,
    bank_list: List,
    matches: List,
    tol: float,
    date_window_days: int = 30,
    max_combo_size: int = 8
):
    """
    DP 子集和凑数：为每个 GL 找一组 Bank 使其金额之和相等
    使用动态规划替代暴力枚举，可处理更大规模
    """
    from src.core.subset_sum import subset_sum_dp_with_fallback

    used_bank = set()
    to_remove_gl = []

    for gi, g in enumerate(gl_list):
        target = abs(g.amount)

        # 构建可用 Bank 列表（排除已使用的）
        available_indices = [i for i in range(len(bank_list)) if i not in used_bank]
        available_amounts = [bank_list[i].amount for i in available_indices]

        if not available_amounts:
            continue

        combo_local, method = subset_sum_dp_with_fallback(
            available_amounts, target, tol, max_combo_size
        )
        
        # 子集和成功后，再检查日期是否满足窗口要求
        if combo_local is not None:
            combo_entries = [bank_list[available_indices[i]] for i in combo_local]
            if not _date_close_group(g, combo_entries, date_window_days):
                combo_local = None  # 日期不满足，放弃这个匹配

        if combo_local is not None:
            # 转回全局索引
            global_combo = [available_indices[i] for i in combo_local]
            combo_entries = [bank_list[i] for i in global_combo]
            matches.append((g, combo_entries, method))
            for i in global_combo:
                used_bank.add(i)
            to_remove_gl.append(gi)

    # 移除已匹配的 GL
    for gi in sorted(to_remove_gl, reverse=True):
        gl_list.pop(gi)

    # 移除已匹配的 Bank
    for bi in sorted(used_bank, reverse=True):
        bank_list.pop(bi)


def _agg_key(bank_entry, level: int) -> tuple:
    """生成聚合键"""
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
    """金额匹配（考虑正负号）"""
    return abs(a - b) <= tol


def _date_close(g, b, max_days: int = 30) -> bool:
    """判断日期是否接近"""
    g_date = getattr(g, 'entry_date', None) or getattr(g, 'tx_date', None)
    b_date = getattr(b, 'tx_date', None) or getattr(b, 'entry_date', None)
    if g_date is None or b_date is None:
        return True
    diff = (g_date - b_date).days
    # GL 记账时间不会早于 Bank 时间
    return 0 <= diff <= max_days


def _date_close_group(g, bank_group, max_days: int = 30) -> bool:
    """判断 GL 日期与 Bank 组内最近日期是否接近"""
    g_date = getattr(g, 'entry_date', None) or getattr(g, 'tx_date', None)
    if g_date is None or not bank_group:
        return True
    # 取 Bank 组内最早和最晚的日期
    dates = [getattr(b, 'tx_date', None) or getattr(b, 'entry_date', None) for b in bank_group]
    dates = [d for d in dates if d is not None]
    if not dates:
        return True
    min_date = min(dates)
    max_date = max(dates)
    # GL 日期应在 Bank 日期之后 max_days 天内
    return 0 <= (g_date - min_date).days <= max_days
