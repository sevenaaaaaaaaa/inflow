"""Insight Flow 数据库方言层（对齐 OpenFlow 双驱动的迁移教训）

OpenFlow 迁移 MySQL 时踩到的三条坑，这里统一处理：
1. `INSERT OR IGNORE`（SQLite）↔ `INSERT IGNORE`（MySQL）—— 不能共用同一句
2. 日期分桶：SQLite `strftime` ↔ MySQL `DATE_FORMAT`（且周格式不同）
3. MySQL 5.7 无部分索引 → 去重索引策略降级（由代码层保证幂等）

另：MySQL 的 `ONLY_FULL_GROUP_BY` 严格模式要求 GROUP BY 覆盖非聚合列——
本项目的聚合查询已遵循该约束（聚合列都在 GROUP BY 中）。
"""

import re


class SQLiteDialect:
    name = "sqlite"
    placeholder = "?"
    supports_partial_index = True

    def adapt(self, sql: str) -> str:
        """占位符适配（SQLite 用 ?，无需转换）"""
        return sql

    def parse_duration(self, expr_sql: str) -> str:
        return expr_sql

    def bucket(self, column: str, fmt: str) -> str:
        """时间桶表达式（SQLite strftime）"""
        return f"strftime('{fmt}', {column})"

    def insert_or_ignore(self, sql: str) -> str:
        return sql  # SQLite 原生 INSERT OR IGNORE

    def now_expr(self) -> str:
        return "datetime('now')"


class MySQLDialect:
    name = "mysql"
    placeholder = "%s"
    supports_partial_index = False   # MySQL 5.7 无部分索引（OpenFlow 教训 #3）

    def adapt(self, sql: str) -> str:
        """`?` → `%s`，并转义 SQL 中字面量 `%`（教训 #4）

        pymysql 用 `query % args` 做参数替换：SQL 里 DATE_FORMAT('%Y-%m-%d') 的 `%Y`
        会被误认为格式符报 "unsupported format character"。
        做法：先占位符转换，再把非占位符的 `%` 全部翻倍（`%%`）。
        """
        sql = sql.replace("?", "%s")
        return "%s".join(part.replace("%", "%%") for part in sql.split("%s"))

    def parse_duration(self, expr_sql: str) -> str:
        return expr_sql

    # 周格式不同：SQLite %W（周一为周首，00-53）↔ MySQL %x-%v（ISO 年-周）
    _FMT_MAP = {
        "%Y-%m-%d %H": "%Y-%m-%d %H",
        "%Y-%m-%d": "%Y-%m-%d",
        "%Y-W%W": "%x-W%v",
    }

    def bucket(self, column: str, fmt: str) -> str:
        mysql_fmt = self._FMT_MAP.get(fmt, fmt)
        return f"DATE_FORMAT({column}, '{mysql_fmt}')"

    def insert_or_ignore(self, sql: str) -> str:
        return re.sub(r"INSERT\s+OR\s+IGNORE", "INSERT IGNORE", sql, flags=re.IGNORECASE)

    def now_expr(self) -> str:
        return "NOW()"


def get_dialect(driver: str):
    return MySQLDialect() if driver == "mysql" else SQLiteDialect()
