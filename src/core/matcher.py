"""主匹配引擎：编排整个核对流程"""
import pandas as pd
from typing import List, Tuple, Dict
from collections import defaultdict

from src.config.field_mapper import FieldMapper
from src.core.cleaner import clean_journal, clean_bank, split_by_detail_account, split_by_month_and_direction
from src.core.block_splitter import split_into_blocks
from src.core.small_block import solve_small_block
from src.core.large_block import solve_large_block


# 小 Block 判定阈值
SMALL_BLOCK_THRESHOLD = 10


class ReconciliationResult:
    """核对结果容器"""
    def __init__(self):
        self.matches: List[Tuple] = []          # (gl, [banks], match_type)
        self.unmatched_gl: List = []
        self.unmatched_bank: List = []
        self.summary: Dict = {}


class ReconciliationEngine:
    """序时账与银行流水核对引擎"""

    def __init__(self, tol: float = 0.01, small_threshold: int = SMALL_BLOCK_THRESHOLD, date_window_days: int = 7):
        self.tol = tol
        self.small_threshold = small_threshold
        self.date_window_days = date_window_days
        self.field_mapper = FieldMapper()

    def _compute_block_date_window(self, gl_block, bank_block) -> int:
        """
        按 block 实际时间跨度计算日期容差。
        规则：取 min(全局日期容差, block 实际日期跨度)，至少 1 天。
        """
        all_dates = []
        for e in gl_block + bank_block:
            d = getattr(e, 'entry_date', None) or getattr(e, 'tx_date', None)
            if d:
                all_dates.append(d)
        if len(all_dates) < 2:
            return self.date_window_days
        span = (max(all_dates) - min(all_dates)).days
        return max(1, min(self.date_window_days, span))

    def load_journal(self, df: pd.DataFrame, manual_map: Dict[str, str] = None) -> List[str]:
        """加载序时账，返回未识别到的必填字段列表（由调用方决定如何处理）"""
        self.field_mapper.auto_map_journal(df)
        if manual_map:
            for k, v in manual_map.items():
                self.field_mapper.set_journal_field(k, v)
        self.journal_df = df
        return self.field_mapper.validate_journal()

    def load_bank(self, df: pd.DataFrame, manual_map: Dict[str, str] = None) -> List[str]:
        """加载银行流水，返回未识别到的必填字段列表"""
        self.field_mapper.auto_map_bank(df)
        if manual_map:
            for k, v in manual_map.items():
                self.field_mapper.set_bank_field(k, v)
        self.bank_df = df
        return self.field_mapper.validate_bank()

    def check_ready(self) -> List[str]:
        """检查是否所有必填字段已配置，返回错误信息列表"""
        errors = []
        journal_missing = self.field_mapper.validate_journal()
        if journal_missing:
            errors.append(f"序时账缺少必填字段: {journal_missing}")
        bank_missing = self.field_mapper.validate_bank()
        if bank_missing:
            errors.append(f"银行流水缺少必填字段: {bank_missing}")
        return errors

    def run(self, detail_account: str = None) -> Dict[str, ReconciliationResult]:
        """
        执行核对流程
        
        如果指定 detail_account，只核对该明细科目；否则全部核对
        返回: {detail_account: ReconciliationResult}
        """
        results, _ = self._run_internal(detail_account)
        return results

    def run_with_summary(self, detail_account: str = None):
        """
        执行核对流程，同时返回月度汇总表和诊断信息
        
        返回: (results, monthly_summary, diagnostics)
        """
        results, diagnostics = self._run_internal(detail_account)
        monthly_summary = self._build_monthly_summary(diagnostics)
        return results, monthly_summary, diagnostics

    def _run_internal(self, detail_account: str = None):
        """内部执行方法，返回 results 和 diagnostics"""
        # 1. 清洗数据
        journal_entries, journal_errors = clean_journal(
            self.journal_df, self.field_mapper.journal_map
        )
        bank_entries, bank_errors = clean_bank(
            self.bank_df, self.field_mapper.bank_map
        )

        # diagnostics 全部用扁平结构，避免嵌套元组导致序列化失败
        diagnostics = {
            "journal_total": len(journal_entries),
            "journal_errors": len(journal_errors),
            "bank_total": len(bank_entries),
            "bank_errors": len(bank_errors),
            "journal_by_account": {},
            "bank_monthly": [],
            "matching_process": [],
        }

        # bank_monthly 展平为列表
        bank_month_dict = {}
        for e in bank_entries:
            k = (e.tx_date.year, e.tx_date.month, 'income' if e.is_income else 'expense')
            bank_month_dict.setdefault(k, {"count": 0, "sum": 0.0})
            bank_month_dict[k]["count"] += 1
            bank_month_dict[k]["sum"] += e.amount
        for (year, month, direction), info in bank_month_dict.items():
            diagnostics["bank_monthly"].append({
                "year": year, "month": month, "direction": direction,
                "count": info["count"], "sum": round(info["sum"], 2),
            })

        # 2. 按明细科目分账
        journal_by_account = split_by_detail_account(journal_entries)

        for acc, entries in journal_by_account.items():
            gl_by_month_dir = split_by_month_and_direction(entries)
            diag_rows = []
            for key, vals in gl_by_month_dir.items():
                (year, month), direction = key
                diag_rows.append({
                    "year": year, "month": month, "direction": direction,
                    "count": len(vals), "sum": round(sum(v.amount for v in vals), 2),
                })
            diagnostics["journal_by_account"][acc] = diag_rows

        results = {}

        accounts = [detail_account] if detail_account else list(journal_by_account.keys())

        for acc in accounts:
            gl_list = journal_by_account.get(acc, [])
            if not gl_list:
                continue

            # 3. 按月份+收支方向拆分
            gl_by_month_dir = split_by_month_and_direction(gl_list)
            bank_by_month_dir = split_by_month_and_direction(bank_entries)

            result = ReconciliationResult()
            account_diag = {"account": acc, "months": []}

            for key, gl_month_entries in gl_by_month_dir.items():
                (year, month), direction = key
                month_info = {
                    "year": year, "month": month, "direction": direction,
                    "gl_count": len(gl_month_entries),
                    "gl_sum": round(sum(e.amount for e in gl_month_entries), 2),
                }
                bank_month_entries = bank_by_month_dir.get(key, [])
                month_info["bank_count"] = len(bank_month_entries)
                month_info["bank_sum"] = round(sum(e.amount for e in bank_month_entries), 2) if bank_month_entries else 0.0

                if not bank_month_entries:
                    result.unmatched_gl.extend(gl_month_entries)
                    month_info["blocks"] = 0
                    month_info["matches"] = 0
                    month_info["block_log"] = []
                    account_diag["months"].append(month_info)
                    continue

                # 4. 最小平扫区域分块
                blocks, block_log = split_into_blocks(gl_month_entries, bank_month_entries, self.tol)
                month_info["blocks"] = len(blocks)
                month_info["block_log"] = block_log.get("blocks", [])

                month_matches = 0
                for gl_block, bank_block in blocks:
                    if not gl_block or not bank_block:
                        result.unmatched_gl.extend(gl_block)
                        result.unmatched_bank.extend(bank_block)
                        continue

                    # 计算当前 block 的日期容差
                    block_date_window = self._compute_block_date_window(gl_block, bank_block)

                    # 5. 判断 Block 大小，分别处理
                    is_small = len(gl_block) <= self.small_threshold and len(bank_block) <= self.small_threshold

                    if is_small:
                        m, ug, ub = solve_small_block(gl_block, bank_block, self.tol, block_date_window)
                    else:
                        m, ug, ub = solve_large_block(gl_block, bank_block, self.tol, block_date_window)

                    result.matches.extend(m)
                    result.unmatched_gl.extend(ug)
                    result.unmatched_bank.extend(ub)
                    month_matches += len(m)

                month_info["matches"] = month_matches
                account_diag["months"].append(month_info)

            diagnostics["matching_process"].append(account_diag)

            # 汇总统计
            result.summary = {
                "detail_account": acc,
                "total_matches": len(result.matches),
                "total_unmatched_gl": len(result.unmatched_gl),
                "total_unmatched_bank": len(result.unmatched_bank),
            }
            results[acc] = result

        return results, diagnostics

    def _build_monthly_summary(self, diagnostics):
        """构建月度总体差异表"""
        rows = []
        for proc in diagnostics.get("matching_process", []):
            acc = proc["account"]
            for m in proc["months"]:
                direction_label = "收入" if m["direction"] == "income" else "支出"
                gl_sum = m["gl_sum"]
                bank_sum = m["bank_sum"]
                diff = round(gl_sum - bank_sum, 2)
                rows.append({
                    "明细科目": acc,
                    "年份": m["year"],
                    "月份": m["month"],
                    "收支": direction_label,
                    "序时账笔数": m["gl_count"],
                    "银行流水笔数": m["bank_count"],
                    "序时账金额": gl_sum,
                    "银行流水金额": bank_sum,
                    "差异": diff,
                    "Block数": m["blocks"],
                    "匹配成功": m["matches"],
                })
        return pd.DataFrame(rows)


def export_results(results: Dict[str, ReconciliationResult], output_path: str):
    """导出核对结果到 Excel"""
    import openpyxl
    from openpyxl.styles import PatternFill, Font

    rows = []
    for acc, res in results.items():
        # 匹配结果
        for gl, banks, mtype in res.matches:
            rows.append({
                "明细科目": acc,
                "类型": "匹配",
                "匹配方式": mtype,
                "GL日期": getattr(gl, 'entry_date', None),
                "GL凭证号": getattr(gl, 'voucher_no', ''),
                "GL摘要": getattr(gl, 'abstract', ''),
                "GL金额": gl.amount,
                "Bank笔数": len(banks),
                "Bank日期": ", ".join(str(getattr(b, 'tx_date', '')) for b in banks),
                "Bank交易方": ", ".join(getattr(b, 'counter_party', '') for b in banks),
                "Bank金额合计": sum(b.amount for b in banks),
            })
        # 未匹配 GL
        for gl in res.unmatched_gl:
            rows.append({
                "明细科目": acc,
                "类型": "未匹配-序时账",
                "匹配方式": "",
                "GL日期": getattr(gl, 'entry_date', None),
                "GL凭证号": getattr(gl, 'voucher_no', ''),
                "GL摘要": getattr(gl, 'abstract', ''),
                "GL金额": gl.amount,
                "Bank笔数": 0,
                "Bank日期": "",
                "Bank交易方": "",
                "Bank金额合计": 0,
            })
        # 未匹配 Bank
        for b in res.unmatched_bank:
            rows.append({
                "明细科目": acc,
                "类型": "未匹配-银行流水",
                "匹配方式": "",
                "GL日期": None,
                "GL凭证号": "",
                "GL摘要": "",
                "GL金额": 0,
                "Bank笔数": 1,
                "Bank日期": getattr(b, 'tx_date', ''),
                "Bank交易方": getattr(b, 'counter_party', ''),
                "Bank金额合计": b.amount,
            })

    df = pd.DataFrame(rows)
    df.to_excel(output_path, index=False)
    print(f"结果已导出: {output_path}")
