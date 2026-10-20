(() => {
  const tg = window.Telegram && window.Telegram.WebApp;
  const $ = (id) => document.getElementById(id);
  const pendingKey = (productId) => `manubis-miniapp-idempotency:${productId}`;
  const ticketPendingKey = 'manubis-miniapp-ticket-pending';
  const initData = tg && tg.initData ? tg.initData : "";
  document.querySelectorAll('[data-tab-target]').forEach((button) => button.addEventListener('click', () => {
    const target = button.dataset.tabTarget;
    document.querySelectorAll('[data-tab-target]').forEach((item) => item.classList.toggle('active', item === button));
    document.querySelectorAll('[data-tab-panel]').forEach((panel) => { panel.hidden = panel.dataset.tabPanel !== target; });
    if (target === 'support') loadTickets().catch(() => { $('ticket-notice').textContent = 'دریافت فهرست تیکت‌ها ناموفق بود؛ دوباره تلاش کن.'; });
  }));
  if (tg) { tg.ready(); tg.expand(); }
  const notice = (message, type = "") => { $('notice').textContent = message; $('notice').className = `notice ${type}`.trim(); };
  const element = (tag, className, text) => { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = String(text); return node; };
  const money = (value, currency) => `${Number(value || 0).toLocaleString('fa-IR')} ${currency || ''}`.trim();
  async function api(path, options = {}) {
    const headers = { Authorization: `tma ${initData}`, ...(options.headers || {}) };
    const response = await fetch(path, { cache: 'no-store', credentials: 'same-origin', ...options, headers });
    let payload = {}; try { payload = await response.json(); } catch (_) {}
    if (!response.ok && response.status !== 202) { const error = new Error(payload.error || 'request_failed'); error.status = response.status; throw error; }
    return { status: response.status, payload };
  }
  function renderWallet(data) {
    const roots = [$('wallet'), $('wallet-summary')].filter(Boolean);
    for (const root of roots) {
      root.replaceChildren();
      const balances = data.balances || {}; const debts = data.debts || {};
      const currencies = new Set([...Object.keys(balances), ...Object.keys(debts)]);
      if (!currencies.size) currencies.add('IRR');
      for (const currency of [...currencies].sort()) {
        const balance = element('div', 'wallet-item'); balance.append(element('span', '', `موجودی ${currency}`), element('strong', '', money(balances[currency], currency))); root.append(balance);
        if (Number(debts[currency] || 0) > 0) { const debt = element('div', 'wallet-item debt'); debt.append(element('span', '', `بدهی ${currency}`), element('strong', '', money(debts[currency], currency))); root.append(debt); }
      }
    }
  }
  function renderTickets(tickets) {
    const root = $('tickets'); root.replaceChildren(); $('ticket-count').textContent = tickets.length.toLocaleString('fa-IR') + ' تیکت';
    const labels = { open: 'باز', waiting_customer: 'منتظر پاسخ شما', waiting_staff: 'در انتظار پشتیبانی', closed: 'بسته‌شده' };
    if (!tickets.length) { root.append(element('div', 'empty', 'هنوز تیکتی ثبت نکرده‌ای. اگر به کمک نیاز داری، فرم بالا را پر کن.')); return; }
    for (const ticket of tickets) {
      const card = element('article', 'card'); card.append(element('h3', '', ticket.subject || 'تیکت پشتیبانی'));
      card.append(element('p', '', 'شماره پیگیری: ' + ticket.ticket_id));
      card.append(element('p', '', 'وضعیت: ' + (labels[ticket.status] || 'نامشخص')));
      card.append(element('p', '', 'آخرین به‌روزرسانی: ' + (ticket.updated_at || ticket.created_at || '—'))); root.append(card);
    }
  }
  async function loadTickets() {
    const result = await api('/api/v1/tickets'); renderTickets(result.payload.tickets || []);
  }
  function ticketIdempotencyKey(subject, message) {
    const fingerprint = subject.trim() + '\\n' + message.trim();
    try {
      const prior = JSON.parse(sessionStorage.getItem(ticketPendingKey) || 'null');
      if (prior && prior.fingerprint === fingerprint && typeof prior.key === 'string') return prior.key;
      const key = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}-ticket`;
      sessionStorage.setItem(ticketPendingKey, JSON.stringify({ fingerprint, key })); return key;
    } catch (_) { throw new Error('ticket_retry_storage_unavailable'); }
  }
  $('support-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const subject = $('ticket-subject').value.trim(); const message = $('ticket-message').value.trim();
    const button = $('ticket-submit'); const status = $('ticket-notice');
    let key;
    try { key = ticketIdempotencyKey(subject, message); }
    catch (_) { status.textContent = 'برای جلوگیری از ثبت تکراری، امکان نگهداری امن شناسه درخواست وجود ندارد.'; return; }
    button.disabled = true; button.textContent = 'در حال ارسال تیکت…'; status.textContent = '';
    try {
      const result = await api('/api/v1/tickets', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ subject, message, idempotency_key: key }) });
      sessionStorage.removeItem(ticketPendingKey); $('support-form').reset();
      status.textContent = `تیکت با موفقیت ثبت شد. شماره پیگیری: ${result.payload.ticket_id}؛ اعلان آن برای مدیر در صف ارسال قرار گرفت.`;
      status.className = 'muted success-text'; await loadTickets();
    } catch (error) {
      status.textContent = error.status === 409 ? 'این درخواست با یک ارسال قبلی تداخل دارد؛ متن را بررسی و دوباره ارسال کن.' : 'نتیجه ارسال قطعی نیست؛ با همان متن دوباره تلاش کن تا تیکت تکراری ساخته نشود.';
      status.className = 'muted error-text';
    } finally { button.disabled = false; button.textContent = 'ارسال تیکت برای پشتیبانی'; }
  });
  function renderProducts(products) {
    const root = $('products'); root.replaceChildren(); $('product-count').textContent = `${products.length.toLocaleString('fa-IR')} محصول`;
    if (!products.length) { root.append(element('div', 'empty', 'فعلاً محصول فعالی برای فروش ثبت نشده است.')); return; }
    for (const product of products) {
      const card = element('article', 'card'); card.append(element('h3', '', product.title || product.product_id));
      if (product.description) card.append(element('p', '', product.description));
      const metered = product.pricing_mode === 'hourly' || product.pricing_mode === 'usage';
      const effectivePrice = metered ? product.unit_price_minor : product.price_minor;
      const price = element('div', 'price', money(effectivePrice, product.currency)); price.append(element('small', '', product.pricing_mode === 'hourly' ? ' · به‌ازای ساعت' : product.pricing_mode === 'usage' ? ' · به‌ازای GiB' : ' · قیمت محصول')); card.append(price);
      const tags = element('div', 'meta');
      if (Number(product.duration_hours || 0) > 0) tags.append(element('span', 'tag', `مدت ${Number(product.duration_hours).toLocaleString('fa-IR')} ساعت`));
      if (Number(product.traffic_bytes || 0) > 0) tags.append(element('span', 'tag', `${(Number(product.traffic_bytes) / (1024 ** 3)).toFixed(0)} GiB`));
      tags.append(element('span', 'tag', product.pricing_mode || 'fixed')); card.append(tags);
      const button = element('button', '', 'خرید با کیف پول');
      button.addEventListener('click', () => confirmPurchase(product, button)); card.append(button); root.append(card);
    }
  }
  function renderServices(services) {
    const root = $('services'); root.replaceChildren(); $('service-count').textContent = `${services.length.toLocaleString('fa-IR')} سرویس`;
    if (!services.length) { root.append(element('div', 'empty', 'هنوز سرویسی برای این حساب ثبت نشده است.')); return; }
    for (const service of services) {
      const card = element('article', 'card'); card.append(element('h3', '', service.username || service.service_id));
      card.append(element('p', '', `وضعیت: ${service.status || 'نامشخص'}`));
      if (service.expires_at) card.append(element('p', '', `انقضا: ${service.expires_at}`));
      if (Number(service.traffic_limit_bytes || 0) > 0) card.append(element('p', '', `مصرف: ${(Number(service.traffic_used_bytes || 0) / (1024 ** 3)).toFixed(2)} / ${(Number(service.traffic_limit_bytes) / (1024 ** 3)).toFixed(2)} GiB`));
      if (service.subscription_url) { const link = element('a', 'service-link', 'بازکردن لینک خصوصی اشتراک'); link.href = service.subscription_url; link.target = '_blank'; link.rel = 'noopener noreferrer'; card.append(link); }
      const actions = element('div', 'service-actions');
      for (const [changeType, label] of [['renewal', 'پیش‌نمایش تمدید'], ['traffic_topup', 'پیش‌نمایش افزایش حجم']]) {
        const button = element('button', 'secondary-button', label);
        button.addEventListener('click', () => previewServiceChange(service, changeType, button, card));
        actions.append(button);
      }
      card.append(actions);
      root.append(card);
    }
  }
  async function previewServiceChange(service, changeType, button, card) {
    let resultNode = card.querySelector('.service-change-preview');
    if (!resultNode) { resultNode = element('div', 'service-change-preview'); card.append(resultNode); }
    button.disabled = true;
    const previousLabel = button.textContent;
    button.textContent = 'در حال بررسی خواندنی…';
    resultNode.textContent = 'در حال بررسی وضعیت فعلی پنل؛ هیچ تغییری اعمال نمی‌شود.';
    try {
      const result = await api('/api/v1/service-change-preview', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ service_id: service.service_id, change_type: changeType }) });
      const preview = result.payload.preview || {};
      const lines = ['پیش‌نمایش خواندنی (بدون برداشت وجه یا تغییر پنل)', `هزینه تخمینی: ${money(preview.amount_minor, preview.currency)}`];
      if (changeType === 'renewal') {
        lines.push(`انقضای فعلی: ${preview.before_expires_at || 'نامشخص'}`);
        lines.push(`انقضای محاسباتی پس از تمدید: ${preview.target_expires_at || 'نامشخص'}`);
      } else {
        lines.push(`سقف فعلی: ${(Number(preview.before_traffic_limit_bytes || 0) / (1024 ** 3)).toFixed(2)} GiB`);
        lines.push(`سقف محاسباتی پس از افزایش: ${(Number(preview.target_traffic_limit_bytes || 0) / (1024 ** 3)).toFixed(2)} GiB`);
      }
      lines.push('این فقط پیش‌نمایش است؛ تأیید و اجرای تغییر در مینی‌اپ هنوز فعال نشده است.');
      resultNode.textContent = lines.join('\n');
    } catch (error) {
      resultNode.textContent = error.status === 409
        ? 'برای این سرویس پیش‌نمایش امن ممکن نیست؛ مالکیت، محصول اصلی یا اتصال پنل را بررسی کنید.'
        : 'دریافت پیش‌نمایش ناموفق بود؛ هیچ مبلغی کسر یا تغییری اعمال نشده است.';
    } finally {
      button.disabled = false;
      button.textContent = previousLabel;
    }
  }
  function renderOrders(orders) {
    const root = $('orders'); root.replaceChildren(); $('order-count').textContent = orders.length.toLocaleString('fa-IR') + ' سفارش';
    const labels = { pending: 'در انتظار پرداخت', paid: 'پرداخت‌شده', provisioning: 'در حال ساخت سرویس', active: 'تکمیل‌شده', failed: 'ناموفق', refunded: 'مستردشده', cancelled: 'لغوشده' };
    if (!orders.length) { root.append(element('div', 'empty', 'هنوز سفارشی برای این حساب ثبت نشده است.')); return; }
    for (const order of orders) {
      const card = element('article', 'card'); card.append(element('h3', '', order.title || order.product_id));
      card.append(element('p', '', 'شناسه سفارش: ' + order.order_id));
      card.append(element('p', '', 'وضعیت: ' + (labels[order.status] || order.status || 'نامشخص')));
      card.append(element('div', 'price', money(order.amount_minor, order.currency)));
      if (Number(order.discount_amount_minor || 0) > 0) card.append(element('p', '', 'تخفیف: ' + money(order.discount_amount_minor, order.currency) + (order.discount_code ? ' · ' + order.discount_code : '')));
      card.append(element('p', '', 'ثبت: ' + (order.created_at || '—'))); root.append(card);
    }
  }
  function renderTransactions(transactions) {
    const root = $('transactions'); root.replaceChildren(); $('transaction-count').textContent = transactions.length.toLocaleString('fa-IR') + ' تراکنش';
    const labels = { deposit: 'واریز کیف پول', purchase: 'خرید', refund: 'بازگشت وجه', reversal: 'اصلاح حساب', metered_hourly: 'هزینه مصرف ساعتی', metered_usage: 'هزینه مصرف حجمی', metered_debt_settlement: 'تسویه بدهی مصرف', reseller_debt_settlement: 'تسویه بدهی نمایندگی', service_change: 'تغییر سرویس', reseller_purchase: 'خرید نمایندگی' };
    if (!transactions.length) { root.append(element('div', 'empty', 'هنوز تراکنشی برای این حساب ثبت نشده است.')); return; }
    for (const transaction of transactions) {
      const card = element('article', 'card'); card.append(element('h3', '', labels[transaction.entry_type] || 'تراکنش کیف پول'));
      const amount = element('div', 'price', (Number(transaction.amount_minor) > 0 ? '+' : '') + money(transaction.amount_minor, transaction.currency));
      if (Number(transaction.amount_minor) < 0) amount.className = 'price debt';
      card.append(amount); card.append(element('p', '', 'تاریخ: ' + (transaction.created_at || '—'))); root.append(card);
    }
  }
  function confirm(message, callback) { if (tg && typeof tg.showConfirm === 'function') tg.showConfirm(message, callback); else callback(window.confirm(message)); }
  async function confirmPurchase(product, button) {
    const keyName = pendingKey(product.product_id);
    let key;
    try {
      key = sessionStorage.getItem(keyName);
      if (!key) { key = (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`); sessionStorage.setItem(keyName, key); }
    } catch (_) { notice('ذخیره امن شناسه تلاش در این مرورگر در دسترس نیست؛ خرید برای جلوگیری از برداشت تکراری متوقف شد.', 'error'); return; }
    const metered = product.pricing_mode === 'hourly' || product.pricing_mode === 'usage';
    const purchasePrice = metered ? product.unit_price_minor : product.price_minor;
    const priceUnit = product.pricing_mode === 'hourly' ? ' به‌ازای ساعت' : product.pricing_mode === 'usage' ? ' به‌ازای GiB' : '';
    confirm(`خرید «${product.title}» به مبلغ ${money(purchasePrice, product.currency)}${priceUnit} از کیف پول انجام شود؟\n\nدر صورت قطع اتصال، تلاش مجدد با همان شناسه انجام می‌شود.`, async (accepted) => {
      if (!accepted) return; button.disabled = true; button.textContent = 'در حال ثبت سفارش…';
      let result;
      try {
        result = await api('/api/v1/purchase', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ product_id: product.product_id, idempotency_key: key }) });
      } catch (error) {
        notice(error.status === 409 ? 'خرید رد شد؛ موجودی، محصول یا تنظیمات پنل را بررسی کنید.' : 'نتیجهٔ درخواست قطعی نیست. در صورت تلاش مجدد همان کلید سفارش حفظ می‌شود.', 'error');
        button.textContent = 'تلاش مجدد با همان شناسه'; button.disabled = false; return;
      }
      if (result.status === 202 || result.payload.status === 'reconciliation_required') { notice('سفارش نیازمند بررسی است؛ برای جلوگیری از خرید تکراری، دوباره ثبت نکنید و از پشتیبانی ربات پیگیری کنید.', 'error'); button.textContent = 'در انتظار بررسی'; button.disabled = true; return; }
      sessionStorage.removeItem(keyName);
      if (result.payload.subscription_url) { notice(`سفارش ثبت شد: ${result.payload.order_id} · لینک اشتراک در بخش سرویس‌های من قرار گرفت.`, 'success'); } else { notice(`سفارش ثبت شد: ${result.payload.order_id}`, 'success'); }
      button.textContent = 'خرید ثبت شد';
      try { const [wallet, services, orders, transactions] = await Promise.all([api('/api/v1/wallet'), api('/api/v1/services'), api('/api/v1/orders'), api('/api/v1/transactions')]); renderWallet(wallet.payload); renderServices(services.payload.services || []); renderOrders(orders.payload.orders || []); renderTransactions(transactions.payload.transactions || []); }
      catch (_) { notice(`سفارش ثبت شد: ${result.payload.order_id}؛ به‌روزرسانی داشبورد ناموفق بود.`, 'success'); }
    });
  }
  async function start() {
    if (!initData) { notice('این پنل باید از داخل ربات تلگرام باز شود؛ نشست معتبر تلگرام در دسترس نیست.', 'error'); return; }
    try {
      const [me, shop, wallet, services, orders, transactions, tickets] = await Promise.all([api('/api/v1/me'), api('/api/v1/shop'), api('/api/v1/wallet'), api('/api/v1/services'), api('/api/v1/orders'), api('/api/v1/transactions'), api('/api/v1/tickets')]);
      $('profile-name').textContent = me.payload.user.username ? `@${me.payload.user.username}` : (me.payload.user.first_name || 'مشتری');
      renderWallet(wallet.payload); renderProducts(shop.payload.products || []); renderServices(services.payload.services || []); renderOrders(orders.payload.orders || []); renderTransactions(transactions.payload.transactions || []); renderTickets(tickets.payload.tickets || []);
      notice('نشست تلگرام تأیید شد. خریدها از کیف پول انجام می‌شوند؛ درگاه پرداخت آنلاین هنوز فعال نیست.', 'success');
    } catch (_) { notice('احراز هویت یا دریافت اطلاعات ناموفق بود. پنل را از داخل تلگرام دوباره باز کنید.', 'error'); }
  }
  start();
})();