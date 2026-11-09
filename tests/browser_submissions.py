"""Browser fault-injection checks against the temporary server in browser_payroll."""
from playwright.sync_api import expect


def check_submissions(page, url):
    def data():
        return page.request.get(url + '/api/data?month=2024-03').json()

    def fill_record(quantity):
        page.locator('#recDate').fill('2024-03-11')
        page.locator('#recPart').fill('重试验证')
        page.locator('#recQty').fill(str(quantity))
        page.locator('#recPrice').fill('2')
        page.locator('#recordSubmit').click()

    def pending():
        return page.evaluate("Object.keys(localStorage).filter(k => k.startsWith('wage.pending.v1.')).map(k => JSON.parse(localStorage.getItem(k)))")

    def lose_response(route):
        assert route.fetch().status == 200
        route.abort()

    def lose_request(route):
        route.abort()

    baseline = len(data()['records'])
    page.route('**/api/records', lose_response)
    fill_record(7)
    expect(page.locator('#saveStatus')).to_contain_text('结果待确认')
    page.unroute('**/api/records', lose_response)
    assert len(data()['records']) == baseline + 1
    page.reload()
    expect(page.locator('#pendingSubmissions')).to_be_visible()
    expect(page.locator('#saveStatus')).to_have_text('账本已加载')
    assert len(data()['records']) == baseline + 1  # Reload is read-only.
    page.get_by_role('button', name='核对结果', exact=True).click()
    expect(page.locator('#pendingSubmissions')).to_be_hidden()
    expect(page.locator('#saveStatus')).to_contain_text('已核对提交结果')
    assert len(data()['records']) == baseline + 1

    page.route('**/api/records', lose_request)
    fill_record(8)
    expect(page.locator('#saveStatus')).to_contain_text('结果待确认')
    page.locator('#recQty').fill('999')
    page.locator('#recordSubmit').click()
    expect(page.locator('#saveStatus')).to_contain_text('原内容未被覆盖')
    assert pending()[0]['payload']['quantity'] == 8
    page.unroute('**/api/records', lose_request)
    page.reload()
    expect(page.locator('#saveStatus')).to_have_text('账本已加载')
    page.get_by_role('button', name='安全重试', exact=True).click()
    expect(page.locator('#pendingSubmissions')).to_be_hidden()
    expect(page.locator('#saveStatus')).to_contain_text('已核对提交结果')
    assert len(data()['records']) == baseline + 2
    assert sorted(row['quantity'] for row in data()['records'] if row['part_name'] == '重试验证') == [7, 8]

    for handler, amount, void in [(lose_response, '12', True), (lose_request, '13', False)]:
        page.locator('#monthSelect').select_option('2024-03')
        page.route('**/api/payments', handler)
        page.locator('#paymentDate').fill('2024-04-10')
        page.locator('#paymentAmount').fill(amount)
        page.locator('#paymentSubmit').click()
        expect(page.locator('#saveStatus')).to_contain_text('结果待确认')
        item = pending()[0]
        if void:
            assert page.request.delete(url + '/api/payments/' + item['id']).status == 200
        page.unroute('**/api/payments', handler)
        page.reload()
        expect(page.locator('#saveStatus')).to_have_text('账本已加载')
        page.get_by_role('button', name='安全重试', exact=True).click()
        expect(page.locator('#pendingSubmissions')).to_be_hidden()
        expect(page.locator('#saveStatus')).to_contain_text('已作废' if void else '已核对提交结果')
        assert data()['reconciliation']['paid'] == (0 if void else 13)

    page.route('**/api/records', lose_request)
    fill_record(9)
    expect(page.locator('#saveStatus')).to_contain_text('结果待确认')
    item = pending()[0]
    page.unroute('**/api/records', lose_request)
    page.get_by_role('button', name='取消本次提交', exact=True).click()
    expect(page.locator('#saveStatus')).to_contain_text('本次提交已取消')
    delayed = dict(item['payload'], request_id=item['id'], submission_epoch=item['epoch'])
    assert page.request.post(url + '/api/records', data=delayed).status == 409
    assert len(data()['records']) == baseline + 2

    snapshot = page.request.get(url + '/api/backup').json()
    page.route('**/api/records', lose_response)
    fill_record(10)
    expect(page.locator('#saveStatus')).to_contain_text('结果待确认')
    page.unroute('**/api/records', lose_response)
    assert page.request.post(url + '/api/restore', data=snapshot).status == 200
    page.reload()
    expect(page.locator('#saveStatus')).to_have_text('账本已加载')
    expect(page.locator('#pendingSubmissions')).to_contain_text('账本已恢复或切换')
    expect(page.get_by_role('button', name='安全重试', exact=True)).to_have_count(0)
    page.get_by_role('button', name='已人工核对，移除旧提示', exact=True).click()
    expect(page.locator('#pendingSubmissions')).to_be_hidden()
    assert len(data()['records']) == baseline + 2

    page.evaluate("() => { Storage.prototype.setItem = function() { throw new Error('storage unavailable'); }; }")
    fill_record(11)
    expect(page.locator('#saveStatus')).to_contain_text('浏览器无法可靠保存')
    assert len(data()['records']) == baseline + 2
    page.reload()
