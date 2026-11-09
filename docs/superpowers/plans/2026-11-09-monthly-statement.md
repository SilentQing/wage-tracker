# Monthly Statement Implementation Plan

**Goal:** 导出用户已确认的完整月结单 CSV。
**Architecture:** 扩展现有 `/api/export` 的 `kind=statement`，在同一数据库读事务中复用 `ledger_data`，CSV 使用 BOM 和现有文本公式防护。
**Tech Stack:** FastAPI、SQLite、Python csv、原生 JavaScript。
**Spec:** 本聊天已批准方案：仅具体月份，包含出勤、工资组成、到账与作废、核对提示及交件/请假/加班明细；不更改核算规则。

## Constraints and review focus
- 保留已有未提交修改，不提交或部署。
- 未确认全勤及待定价必须提示；旧考勤请假未知不能写成零。
- 跨月到账按工资所属月份归集；跨月周的加班不得混入月结单。
- 作废到账保留但不计合计；删除交件不参与月结单。
- 金额与接口一致，逐条舍入；文本防公式注入。

## Steps
- [x] 新增 tests/test_statement.py，验证核算、范围、提示、公式防护与无效月份。
- [x] app.py 扩展 export_csv 并增加 write_monthly_statement(writer, data, legacy, month)。
- [x] templates/index.html 添加入口；static/app.js 在全部累计时提示选择月份。
- [x] README.md 补充说明，运行完整后端测试和 JS 语法检查。

## Verification
2026-11-09：新增3项测试先因接口尚不支持 statement 返回422而失败；实现后完整47项后端测试通过。node --check static/app.js 与 git diff --check 通过。未部署、未修改真实账本，未运行浏览器或 Excel/WPS 人工打开验证。沙箱临时目录权限错误后，经批准在沙箱外执行测试。
