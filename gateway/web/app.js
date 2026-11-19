'use strict';
const $ = (selector) => document.querySelector(selector);
const state = {token: sessionStorage.getItem('parking-token'), user: null, view: 'parking',
  lots: [], vehicles: [], bookings: [], notifications: [], lotId: null, spaceId: null,
  availability: null, register: false, paymentBooking: null, paymentKey: null,
  promoCode:'', promoQuote:null, waitlist:[], reportRange:null};
const views = {
  parking: ['Найдите своё место', 'Выберите парковку и забронируйте место заранее.', 'Парковки / Поиск места'],
  bookings: ['Ваши бронирования', 'Все предстоящие поездки и история парковок.', 'Личный кабинет / Бронирования'],
  payments: ['История платежей', 'Учебные оплаты и автоматические возвраты.', 'Личный кабинет / Платежи'],
  profile: ['Профиль и автомобили', 'Сохраните автомобиль для быстрого бронирования.', 'Личный кабинет / Профиль'],
  notifications: ['Уведомления', 'События ваших бронирований и платежей.', 'Личный кабинет / Уведомления'],
  admin: ['Управление парковками', 'Места, тарифы и бронирования пользователей.', 'Администратор / Управление'],
  waitlist: ['Лист ожидания', 'Заявки на занятые интервалы и предложения свободных мест.', 'Личный кабинет / Лист ожидания'],
  promotions: ['Промокоды', 'Скидки, сроки действия и лимиты применений.', 'Администратор / Промокоды'],
  maintenance: ['Обслуживание парковок', 'Запланируйте временное закрытие парковочного места.', 'Администратор / Обслуживание'],
  reports: ['Отчёты', 'Брони, подтверждённые часы, учебные оплаты и возвраты.', 'Администратор / Отчёты']
};
const statuses = {PENDING: 'Ожидает оплаты', CONFIRMED: 'Подтверждено', CANCELLED: 'Отменено',
  EXPIRED: 'Срок истёк', SUCCEEDED: 'Оплачено', DECLINED: 'Отклонено', REFUNDED: 'Возвращено',
  WAITING:'В очереди', OFFERED:'Место предложено', FULFILLED:'Бронь создана',
  ACTIVE:'Запланировано', CANCELLING:'Отменяется', COMPLETED:'Завершено', REJECTED:'Отклонено'};
const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (ch) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const money = (cents) => new Intl.NumberFormat('ru-RU', {style:'currency',currency:'MDL',maximumFractionDigits:2}).format(cents / 100);
const date = (value) => new Date(value).toLocaleString('ru-RU', {day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
const badge = (status) => `<span class="badge ${Object.hasOwn(statuses,status)?status:''}">${escapeHtml(statuses[status] || status)}</span>`;
function localInput(value) { return new Date(value.getTime() - value.getTimezoneOffset()*60000).toISOString().slice(0,16); }
const initial = new Date(Math.ceil((Date.now()+3600000)/900000)*900000);
state.start = localInput(initial); state.end = localInput(new Date(initial.getTime()+2*3600000));

function toast(message) {
  $('#toast').textContent = message; $('#toast').classList.remove('hidden');
  clearTimeout(toast.timer); toast.timer = setTimeout(() => $('#toast').classList.add('hidden'), 4200);
}
function alertError(error) {
  $('#alert').classList.remove('hidden');
  $('#alert').textContent = error.message || 'Не удалось выполнить операцию';
  if (error.requestId) { const small=document.createElement('small'); small.textContent='Номер запроса: '+error.requestId; $('#alert').append(small); }
}
async function api(path, options={}) {
  const controller = new AbortController(); const timeout = setTimeout(() => controller.abort(), 14000);
  try {
    const response = await fetch('/api'+path, {method:options.method || 'GET',signal:controller.signal,
      headers:{...(state.token?{Authorization:'Bearer '+state.token}:{}), ...(options.body?{'Content-Type':'application/json'}:{})},
      ...(options.body?{body:JSON.stringify(options.body)}:{})});
    const result = await response.json();
    if (!response.ok) {
      if (response.status===401 && state.token) logout(false);
      const error = new Error(result.error?.message || 'Ошибка запроса');
      error.requestId = result.error?.request_id; error.status = response.status; throw error;
    }
    return result;
  } catch(error) {
    if (error.name==='AbortError') throw new Error('Сервер не ответил вовремя. Повторите запрос.');
    if (error instanceof TypeError) throw new Error('Нет соединения с сервером. Проверьте запуск проекта.');
    throw error;
  } finally { clearTimeout(timeout); }
}
async function busy(button, fn) {
  if (button.disabled) return; button.disabled=true;
  try { await fn(); } catch(error) { alertError(error); } finally { button.disabled=false; }
}
function account() {
  $('#account-name').textContent=state.user?.name || 'Гость';
  $('#auth-button').textContent=state.user?'Выйти':'Войти';
  document.querySelectorAll('[data-admin-view]').forEach(el=>el.classList.toggle('hidden',state.user?.role!=='admin'));
}
function logout(render=true) {
  sessionStorage.removeItem('parking-token'); state.token=null; state.user=null;
  state.vehicles=[]; state.notifications=[]; state.waitlist=[]; state.promoCode=''; state.promoQuote=null; account(); $('#unread').classList.add('hidden');
  if(render) navigate('parking');
}
function setAuthMode(register) {
  state.register=register;$('#name-label').classList.toggle('hidden',!register);$('#name-label input').required=register;
  $('#auth-title').textContent=register?'Создайте аккаунт':'С возвращением';$('#auth-submit').textContent=register?'Зарегистрироваться':'Войти';
  $('#auth-toggle').textContent=register?'Уже есть аккаунт? Войти':'Нет аккаунта? Зарегистрироваться';$('#auth-error').textContent='';
  $('#auth-form input[name="password"]').autocomplete=register?'new-password':'current-password';
}
function openAuth() { setAuthMode(false); $('#auth-dialog').showModal(); }
async function navigate(view) {
  if (view!=='parking' && !state.user) { openAuth(); return; }
  if (['admin','promotions','maintenance','reports'].includes(view) && state.user?.role!=='admin') return;
  state.view=view; $('#alert').classList.add('hidden');
  document.querySelectorAll('[data-view]').forEach(x => x.classList.toggle('active',x.dataset.view===view));
  const info=views[view]; $('#page-title').textContent=info[0]; $('#page-subtitle').textContent=info[1]; $('#breadcrumb').textContent=info[2];
  $('#content').innerHTML='<div class="empty">Загружаем данные…</div>';
  try {
    if(view==='parking') await parkingView();
    if(view==='bookings') await bookingsView();
    if(view==='payments') await paymentsView();
    if(view==='profile') await profileView();
    if(view==='notifications') await notificationsView();
    if(view==='admin') await adminView();
    if(view==='waitlist') await waitlistView();
    if(view==='promotions') await promotionsView();
    if(view==='maintenance') await maintenanceView();
    if(view==='reports') await reportsView();
  } catch(error) { $('#content').innerHTML='<div class="empty">Данные временно недоступны. Попробуйте открыть раздел ещё раз.</div>'; alertError(error); }
}
async function parkingView() {
  [state.lots,state.vehicles]=await Promise.all([api('/parking/lots'), state.user?api('/profiles/me/vehicles'):Promise.resolve([])]);
  renderParking();
}
function renderParking() {
  const cards=state.lots.map(lot=>`<article class="lot-card ${lot.id===state.lotId?'selected':''}">
    <div class="lot-visual" aria-hidden="true"><div class="parking-lines"><i></i><i class="car"></i><i></i></div></div>
    <div class="lot-content"><h3>${escapeHtml(lot.name)}</h3><div class="lot-address">${escapeHtml(lot.address)}</div>
      <div class="lot-meta"><span class="tag">${lot.active_spaces} мест</span><span class="tag">24 / 7</span><span class="tag">${lot.active?'Открыта':'Закрыта'}</span></div>
      <div class="lot-bottom"><div class="price">${escapeHtml(money(lot.hourly_rate_cents))}<small> / час</small></div>
      <button class="button secondary small" data-lot="${escapeHtml(lot.id)}" ${lot.active?'':'disabled'}>${lot.id===state.lotId?'Выбрана ✓':'Выбрать →'}</button></div>
    </div></article>`).join('');
  $('#content').innerHTML=`<form class="panel filter-row" id="search-form">
    <label>Начало парковки<input id="start-at" type="datetime-local" required value="${escapeHtml(state.start)}"></label>
    <label>Окончание<input id="end-at" type="datetime-local" required value="${escapeHtml(state.end)}"></label>
    <label>Ваш автомобиль<select id="vehicle-select">${state.vehicles.length?state.vehicles.map(v=>`<option value="${escapeHtml(v.id)}" ${v.id===state.vehicleId?'selected':''}>${escapeHtml(v.plate)} · ${escapeHtml(v.label)}</option>`).join(''):'<option value="">Добавьте автомобиль в профиле</option>'}</select></label>
    <button class="button" type="submit">Проверить места →</button></form>
    <div class="section-heading"><h2>Парковки</h2><span class="muted">${state.lots.length} локации</span></div>
    <div class="lot-grid">${cards}</div><div id="availability-area"></div>`;
  $('#search-form').addEventListener('submit',event=>{event.preventDefault(); busy(event.submitter,loadAvailability);});
  for(const input of [$('#start-at'),$('#end-at')]) input.addEventListener('change',()=>{
    state.start=$('#start-at').value; state.end=$('#end-at').value; state.availability=null; state.spaceId=null; state.promoQuote=null; renderAvailability();
  });
  $('#vehicle-select').addEventListener('change',()=>state.vehicleId=$('#vehicle-select').value);
  if(state.vehicles.length && !state.vehicles.some(v=>v.id===state.vehicleId)) state.vehicleId=state.vehicles[0].id;
  renderAvailability();
}
async function loadAvailability(exactRange) {
  state.start=$('#start-at').value; state.end=$('#end-at').value;
  if(!state.lotId) state.lotId=state.lots.find(x=>x.active)?.id;
  if(!state.lotId) throw new Error('Нет доступных парковок');
  const start=new Date(state.start), end=new Date(state.end);
  if(!Number.isFinite(start.getTime()) || !Number.isFinite(end.getTime())) throw new Error('Укажите начало и окончание парковки');
  state.availability=await api('/bookings/availability?'+new URLSearchParams({lot_id:state.lotId,
    start_at:exactRange?.start_at || start.toISOString(),end_at:exactRange?.end_at || end.toISOString()}));
  state.promoQuote=null;
  if(!state.availability.spaces.some(s=>s.id===state.spaceId&&s.available)) state.spaceId=null;
  renderParking();
}
function renderAvailability() {
  const area=$('#availability-area'); if(!area) return;
  if(!state.availability) {area.innerHTML='<p class="hint">Выберите парковку и проверьте свободные места на нужное время.</p>';return;}
  const data=state.availability, selected=data.spaces.find(x=>x.id===state.spaceId);
  area.innerHTML=`<div class="section-heading"><h2>Выберите место · ${escapeHtml(data.lot.name)}</h2><span class="muted">${data.spaces.filter(x=>x.available).length} свободно</span></div>
    <div class="panel space-layout"><div><div class="space-grid">${data.spaces.map(s=>`<button class="space ${s.id===state.spaceId?'selected':''} ${s.maintenance?'under-maintenance':''}" data-space="${escapeHtml(s.id)}" ${s.available?'':'disabled'} title="${s.maintenance?'На обслуживании':(s.kind==='ev'?'Место с зарядкой':'Стандартное место')}">${escapeHtml(s.code)}${s.maintenance?' · ремонт':s.kind==='ev'?' ⚡':''}</button>`).join('')}</div>
    <div class="legend"><span><i></i>Свободно</span><span><i class="busy"></i>Занято / закрыто</span><span><i class="repair"></i>Обслуживание</span></div>${waitlistButton(data)}</div>
    <div class="summary"><h3>Ваше бронирование</h3><div class="summary-row"><span>Место</span><strong>${escapeHtml(selected?.code || 'Выберите на схеме')}</strong></div>
    <div class="summary-row"><span>Начало</span><strong>${date(data.start_at)}</strong></div><div class="summary-row"><span>Окончание</span><strong>${date(data.end_at)}</strong></div>
    ${selected?promotionFields():''}
    <div class="summary-row total"><span>Итого</span><strong id="quoted-total">${escapeHtml(money(state.promoQuote?.amount_cents ?? data.amount_cents))}</strong></div>
    <button class="button full" id="book-button" ${selected?'':'disabled'}>Забронировать место →</button>
    <p class="hint">Место резервируется на 10 минут для оплаты. Тариф применяется за каждый начатый час.</p>
    ${state.user&&!state.vehicles.length?'<button class="text-link" data-view="profile">Сначала добавьте автомобиль →</button>':''}</div></div>`;
  $('#promo-code')?.addEventListener('input', e=>{
    state.promoCode=e.target.value; state.promoQuote=null;
    $('#promo-feedback').textContent=''; $('#quoted-total').textContent=money(data.amount_cents);
  });
}
async function createBooking() {
  if(!state.user){openAuth();return;}
  if(!state.vehicles.length){await navigate('profile');toast('Добавьте автомобиль для бронирования');return;}
  const body={space_id:state.spaceId,vehicle_id:state.vehicleId,start_at:state.availability.start_at,end_at:state.availability.end_at};
  const promo=state.promoCode.trim().toUpperCase();
  if(promo) body.promo_code=promo;
  const fingerprint=JSON.stringify(body); let intent;
  try {intent=JSON.parse(sessionStorage.getItem('parking-booking-intent'));}catch{intent=null;}
  if(!intent || intent.fingerprint!==fingerprint) {intent={fingerprint,key:crypto.randomUUID()};sessionStorage.setItem('parking-booking-intent',JSON.stringify(intent));}
  const booking=await api('/bookings',{method:'POST',body:{...body,idempotency_key:intent.key}});
  sessionStorage.removeItem('parking-booking-intent'); state.availability=null;state.spaceId=null;state.promoCode='';state.promoQuote=null;
  toast('Место зарезервировано на 10 минут'); await navigate('bookings'); openPayment(booking);
}
async function bookingsView() {
  state.bookings=await api('/bookings');
  if(!state.bookings.length){$('#content').innerHTML='<div class="empty">У вас пока нет бронирований. Выберите парковку, чтобы запланировать поездку.</div>';return;}
  $('#content').innerHTML=`<div class="panel table-wrap"><table class="data-table"><thead><tr><th>Парковка / место</th><th>Время</th><th>Автомобиль</th><th>Сумма</th><th>Статус</th><th>Действия</th></tr></thead><tbody>${state.bookings.map(b=>`<tr><td><strong>${escapeHtml(b.lot_name)}</strong><small>Место ${escapeHtml(b.space_code)} · ${escapeHtml(b.id.slice(0,8))}</small></td>
    <td>${date(b.start_at)}<small>до ${date(b.end_at)}</small></td><td>${escapeHtml(b.plate)}</td><td>${escapeHtml(money(b.amount_cents))}${b.promo_code?`<small>${escapeHtml(b.promo_code)} · скидка ${escapeHtml(money(b.discount_cents))}</small>`:''}</td><td>${badge(b.status)}${b.status==='PENDING'?'<small>Оплатить до '+date(b.expires_at)+'</small>':''}</td>
    <td><div class="actions">${b.status==='PENDING'?`<button class="button small" data-pay="${escapeHtml(b.id)}">Оплатить</button>`:''}${['PENDING','CONFIRMED'].includes(b.status)&&new Date(b.start_at)>new Date()?`<button class="button secondary small" data-cancel="${escapeHtml(b.id)}">Отменить</button>`:''}</div></td></tr>`).join('')}</tbody></table></div>`;
}
function openPayment(booking) {
  state.paymentBooking=booking;state.paymentKey=crypto.randomUUID();$('#payment-error').textContent='';$('#payment-outcome').value='success';
  $('#payment-info').innerHTML=`<h3>${escapeHtml(booking.lot_name)} · ${escapeHtml(booking.space_code)}</h3><p>${date(booking.start_at)} - ${date(booking.end_at)}</p><div class="summary-row total"><span>К оплате</span><strong>${escapeHtml(money(booking.amount_cents))}</strong></div>`;
  $('#payment-dialog').showModal();
}
async function paymentsView() {
  const data=await api('/payments');
  $('#content').innerHTML=data.length?`<div class="demo-note">Все операции выполняются в учебном режиме.</div><div class="panel table-wrap"><table class="data-table"><thead><tr><th>Платёж</th><th>Бронирование</th><th>Дата</th><th>Сумма</th><th>Статус</th></tr></thead><tbody>${data.map(p=>`<tr><td>${escapeHtml(p.id.slice(0,8))}</td><td>${escapeHtml(p.booking_id.slice(0,8))}</td><td>${date(p.created_at)}</td><td>${escapeHtml(money(p.amount_cents))}</td><td>${badge(p.status)}${p.refunded_at?'<small>'+date(p.refunded_at)+'</small>':''}</td></tr>`).join('')}</tbody></table></div>`:'<div class="empty">Здесь появятся ваши учебные платежи и возвраты.</div>';
}
async function profileView() {
  const [profile,vehicles]=await Promise.all([api('/profiles/me'),api('/profiles/me/vehicles')]);state.vehicles=vehicles;
  $('#content').innerHTML=`<div class="profile-grid"><form class="panel" id="profile-form"><h2>Личные данные</h2><label>Имя<input name="name" required minlength="2" maxlength="80" value="${escapeHtml(profile.name)}"></label>
    <label>Электронная почта<input type="email" disabled value="${escapeHtml(profile.email)}"></label><label>Телефон<input name="phone" maxlength="30" value="${escapeHtml(profile.phone)}" placeholder="+373 …"></label>
    <label class="check-label"><input name="notifications" type="checkbox" ${profile.notifications_enabled?'checked':''}>Получать новые уведомления</label><button class="button full">Сохранить изменения</button></form>
    <div class="panel"><h2>Ваши автомобили</h2><div>${vehicles.map(v=>`<div class="vehicle-card"><div><span class="plate">${escapeHtml(v.plate)}</span><p class="muted">${escapeHtml(v.label)}</p></div><button class="button secondary small" data-delete-vehicle="${escapeHtml(v.id)}">Удалить</button></div>`).join('') || '<p class="muted">Добавьте первый автомобиль.</p>'}</div>
    <form id="vehicle-form"><div class="form-grid"><label>Номер автомобиля<input name="plate" required minlength="2" maxlength="16" placeholder="ABC 123"></label><label>Марка / название<input name="label" required maxlength="60" placeholder="Мой автомобиль"></label></div><button class="button full">Добавить автомобиль</button></form></div></div>`;
  $('#profile-form').addEventListener('submit',e=>{e.preventDefault();busy(e.submitter,async()=>{
    const f=e.target;const profile=await api('/profiles/me',{method:'PATCH',body:{name:f.elements.name.value,phone:f.elements.phone.value,notifications_enabled:f.elements.notifications.checked}});
    state.user.name=profile.name;account();toast('Профиль сохранён');
  });});
  $('#vehicle-form').addEventListener('submit',e=>{e.preventDefault();busy(e.submitter,async()=>{
    await api('/profiles/me/vehicles',{method:'POST',body:{plate:e.target.elements.plate.value,label:e.target.elements.label.value}});await profileView();toast('Автомобиль добавлен');
  });});
}
async function refreshNotifications() {
  if(!state.user)return;
  state.notifications=await api('/notifications');const count=state.notifications.filter(n=>!n.is_read).length;
  $('#unread').textContent=count;$('#unread').classList.toggle('hidden',count===0);
}
async function notificationsView() {
  await refreshNotifications();
  $('#content').innerHTML=state.notifications.length?`<div class="panel">${state.notifications.map(n=>`<article class="notification ${n.is_read?'':'unread'}"><div><h3>${escapeHtml(n.title)}</h3><p>${escapeHtml(n.message)}</p><time>${date(n.created_at)}</time></div>${n.is_read?'':`<button class="button secondary small" data-read="${escapeHtml(n.id)}">Прочитано</button>`}</article>`).join('')}</div>`:'<div class="empty">Новых уведомлений пока нет.</div>';
}
async function adminView() {
  const [lots,bookings,payments]=await Promise.all([api('/parking/lots'),api('/bookings/admin/all'),api('/payments/admin/all')]);
  const spaces=await Promise.all(lots.map(l=>api('/parking/lots/'+l.id+'/spaces')));
  $('#content').innerHTML=`<div class="panel"><h2>Добавить парковку</h2><form class="admin-form" id="lot-form"><label>Название<input name="name" required minlength="3" maxlength="100"></label><label>Адрес<input name="address" required minlength="5" maxlength="200"></label>
    <label>Тариф, MDL / час<input name="rate" type="number" required min="1" max="1000" step="0.01" value="30"></label><label>Количество мест<input name="capacity" type="number" required min="1" max="200" value="10"></label><button class="button">Создать парковку</button></form></div>
    <div class="panel"><h2>Парковки и места</h2><p class="muted">Нажмите на место, чтобы открыть или закрыть его для новых бронирований.</p>${lots.map((l,i)=>`<div class="admin-lot"><div class="admin-row"><div><h3>${escapeHtml(l.name)}</h3><span class="muted">${escapeHtml(l.address)}</span></div><label>MDL / час<input data-rate="${escapeHtml(l.id)}" type="number" min="1" max="1000" step="0.01" value="${l.hourly_rate_cents/100}"></label>
    <button class="button secondary small" data-save-lot="${escapeHtml(l.id)}" data-active="${l.active}">Сохранить тариф</button><button class="button secondary small" data-toggle-lot="${escapeHtml(l.id)}" data-active="${l.active}">${l.active?'Закрыть парковку':'Открыть парковку'}</button></div>
    <div class="space-grid admin-spaces">${spaces[i].map(s=>`<button class="space ${s.active?'':'selected'}" data-toggle-space="${escapeHtml(s.id)}" data-active="${s.active}">${escapeHtml(s.code)} ${s.active?'✓':'×'}</button>`).join('')}</div></div>`).join('')}</div>
    <div class="panel"><h2>Все бронирования · ${bookings.length}</h2><div class="table-wrap"><table class="data-table"><thead><tr><th>Парковка</th><th>Номер авто</th><th>Время</th><th>Статус</th></tr></thead><tbody>${bookings.map(b=>`<tr><td>${escapeHtml(b.lot_name)} · ${escapeHtml(b.space_code)}</td><td>${escapeHtml(b.plate)}</td><td>${date(b.start_at)}</td><td>${badge(b.status)}</td></tr>`).join('')}</tbody></table></div><p class="hint">Учебных платежей: ${payments.length}. Из них возвратов: ${payments.filter(p=>p.status==='REFUNDED').length}.</p></div>`;
  $('#lot-form').addEventListener('submit',e=>{e.preventDefault();busy(e.submitter,async()=>{const f=e.target;await api('/parking/lots',{method:'POST',body:{name:f.elements.name.value,address:f.elements.address.value,hourly_rate_cents:Math.round(Number(f.elements.rate.value)*100),capacity:Number(f.elements.capacity.value)}});await adminView();toast('Парковка создана');});});
}
document.addEventListener('click',event=>{
  const button=event.target.closest('button');if(!button)return;
  if(button.dataset.view){navigate(button.dataset.view);return;}
  if(button.dataset.close){$('#'+button.dataset.close).close();return;}
  if(button.dataset.lot){busy(button,async()=>{state.lotId=button.dataset.lot;state.spaceId=null;await loadAvailability();});return;}
  if(button.dataset.space){state.spaceId=button.dataset.space;renderAvailability();return;}
  if(button.id==='book-button'){busy(button,createBooking);return;}
  if(button.dataset.pay){const booking=state.bookings.find(b=>b.id===button.dataset.pay);if(booking)openPayment(booking);return;}
  if(button.dataset.cancel){if(!confirm('Отменить бронирование? Учебная оплата будет возвращена.'))return;busy(button,async()=>{await api('/bookings/'+button.dataset.cancel+'/cancel',{method:'POST'});await bookingsView();toast('Бронирование отменено');});return;}
  if(button.dataset.deleteVehicle){busy(button,async()=>{await api('/profiles/me/vehicles/'+button.dataset.deleteVehicle,{method:'DELETE'});await profileView();});return;}
  if(button.dataset.read){busy(button,async()=>{await api('/notifications/'+button.dataset.read+'/read',{method:'POST'});await notificationsView();});return;}
  if(button.dataset.saveLot||button.dataset.toggleLot){busy(button,async()=>{const id=button.dataset.saveLot||button.dataset.toggleLot;const active=button.dataset.active==='true';const rate=Number(document.querySelector(`[data-rate="${CSS.escape(id)}"]`).value);await api('/parking/lots/'+id,{method:'PATCH',body:{hourly_rate_cents:Math.round(rate*100),active:button.dataset.toggleLot?!active:active}});await adminView();toast('Парковка обновлена');});return;}
  if(button.dataset.toggleSpace){busy(button,async()=>{await api('/parking/spaces/'+button.dataset.toggleSpace,{method:'PATCH',body:{active:button.dataset.active!=='1'&&button.dataset.active!=='true'}});await adminView();toast('Доступность места обновлена');});}
});
$('#auth-button').addEventListener('click',()=>state.user?logout():openAuth());
$('#auth-toggle').addEventListener('click',()=>{
  setAuthMode(!state.register);
});
$('#auth-form').addEventListener('submit',async event=>{
  event.preventDefault();const button=event.submitter;button.disabled=true;$('#auth-error').textContent='';
  try{const form=event.target;const body={email:form.elements.email.value,password:form.elements.password.value};if(state.register)body.name=form.elements.name.value;
    const result=await api('/auth/'+(state.register?'register':'login'),{method:'POST',body});state.token=result.access_token;state.user=result.user;sessionStorage.setItem('parking-token',state.token);form.reset();account();$('#auth-dialog').close();toast('Вы вошли в систему');await navigate(state.view);await refreshNotifications();
  }catch(error){$('#auth-error').textContent=error.message;}finally{button.disabled=false;}
});
$('#payment-submit').addEventListener('click',async()=>{
  const button=$('#payment-submit');button.disabled=true;$('#payment-error').textContent='';
  try{const payment=await api('/payments',{method:'POST',body:{booking_id:state.paymentBooking.id,idempotency_key:state.paymentKey,outcome:$('#payment-outcome').value}});
    $('#payment-dialog').close();toast(payment.status==='DECLINED'?'Оплата отклонена. Можно повторить попытку.':'Оплата принята. Подтверждение бронирования появится через несколько секунд.');await navigate('bookings');
  }catch(error){$('#payment-error').textContent=error.message;}finally{button.disabled=false;}
});
$('#docs-button').addEventListener('click',()=>{
  $('#docs-list').innerHTML=Object.entries({auth:'Авторизация',profile:'Профили и автомобили',parking:'Парковки',booking:'Бронирования',payment:'Платежи',notification:'Уведомления',promotion:'Промокоды',waitlist:'Лист ожидания',maintenance:'Обслуживание',reporting:'Отчёты'}).map(([key,label])=>`<a class="docs-link" target="_blank" rel="noopener" href="/api/docs/${key}">${label} ↗</a>`).join('');$('#docs-dialog').showModal();
});
async function connection() {
  try{const status=await api('/status');$('#connection-text').textContent=status.ready?'Все сервисы готовы':'Часть сервисов недоступна';$('#connection-dot').className='status-dot '+(status.ready?'ready':'error');}
  catch{$('#connection-text').textContent='Нет соединения с сервером';$('#connection-dot').className='status-dot error';}
}
async function init(){
  if(state.token){try{state.user=await api('/auth/me');}catch{logout(false);}}
  account();await navigate('parking');await connection();
  if(state.user)try{await refreshNotifications();}catch(error){alertError(error);}
}
setInterval(async()=>{
  if(document.hidden)return;await connection();
  if(state.user)try{await refreshNotifications();if(state.view==='bookings'&&!$('#payment-dialog').open)await bookingsView();if(state.view==='payments')await paymentsView();if(state.view==='notifications')await notificationsView();if(state.view==='waitlist')await waitlistView();}catch{/* Full errors are shown on user-initiated requests. */}
},8000);
init();
