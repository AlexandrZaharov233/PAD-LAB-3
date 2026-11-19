'use strict';

function promotionFields() {
  return `<div class="promo-fields"><label for="promo-code">Промокод</label>
    <div class="inline-action"><input id="promo-code" maxlength="32" placeholder="Например, PARK10" value="${escapeHtml(state.promoCode)}">
    <button type="button" class="button secondary small" id="apply-promo">Применить</button></div>
    <p id="promo-feedback" class="hint">${state.promoQuote?'Скидка: '+escapeHtml(money(state.promoQuote.discount_cents)):''}</p></div>`;
}

async function applyPromotion() {
  if (!state.user) { openAuth(); return; }
  const code = state.promoCode.trim().toUpperCase();
  if (!code) throw new Error('Введите промокод');
  const data = state.availability;
  const quoted = await api('/promotions/quote',{method:'POST',body:{code,lot_id:data.lot.id,base_amount_cents:data.amount_cents}});
  if (state.availability!==data || code!==state.promoCode.trim().toUpperCase() || state.view!=='parking') return;
  state.promoQuote=quoted;
  state.promoCode=quoted.code;
  renderAvailability();
}

function waitlistButton(data) {
  if (!data.lot.active || data.spaces.some(s=>s.available)) return '';
  return '<p class="hint">На это время свободных мест нет.</p><button class="button secondary" id="join-waitlist">Встать в лист ожидания</button>';
}

async function joinWaitlist() {
  if (!state.user) { openAuth(); return; }
  const data = state.availability;
  await api('/waitlist',{method:'POST',body:{lot_id:data.lot.id,start_at:data.start_at,end_at:data.end_at}});
  await navigate('waitlist');
  toast('Заявка в листе ожидания');
}

async function waitlistView() {
  const rows=await api('/waitlist');
  if (state.view!=='waitlist') return;
  state.waitlist=rows;
  $('#content').innerHTML=rows.length
    ? `<div class="demo-note">Предложение действует до 10 минут. Место закрепляется после оформления брони.</div>
       <div class="panel table-wrap"><table class="data-table"><thead><tr><th>Парковка</th><th>Время</th><th>Статус</th><th>Действия</th></tr></thead><tbody>
       ${rows.map(w=>`<tr><td><strong>${escapeHtml(w.lot_name)}</strong>${w.space_code?'<small>Место '+escapeHtml(w.space_code)+'</small>':''}</td>
         <td>${date(w.start_at)}<small>до ${date(w.end_at)}</small></td><td>${badge(w.status)}
         ${w.status==='OFFERED'?'<small>Ответьте до '+date(w.offer_until)+'</small>':''}</td>
         <td><div class="actions">${w.status==='OFFERED'?`<button class="button small" data-wait-offer="${escapeHtml(w.id)}">Выбрать место</button>`:''}
         ${['WAITING','OFFERED'].includes(w.status)?`<button class="button secondary small" data-wait-cancel="${escapeHtml(w.id)}">Отказаться</button>`:''}</div></td></tr>`).join('')}</tbody></table></div>`
    : '<div class="empty">Заявок пока нет. Встать в очередь можно при поиске парковки, если все места заняты.</div>';
}

async function openOffer(id) {
  const item=state.waitlist.find(w=>w.id===id && w.status==='OFFERED');
  if (!item) throw new Error('Предложение больше не действует');
  state.lotId=item.lot_id;
  state.spaceId=item.offered_space_id;
  state.start=localInput(new Date(item.start_at));
  state.end=localInput(new Date(item.end_at));
  state.availability=null;
  await navigate('parking');
  await loadAvailability(item);
  if (!state.spaceId) toast('Предложенное место уже недоступно. Проверьте остальные места.');
}

function lotOptions(lots, includeAll=false) {
  return (includeAll?'<option value="">Все парковки</option>':'')+
    lots.map(l=>`<option value="${escapeHtml(l.id)}">${escapeHtml(l.name)}</option>`).join('');
}

async function promotionsView() {
  const [rows,lots]=await Promise.all([api('/promotions'),api('/parking/lots')]);
  if (state.view!=='promotions') return;
  $('#content').innerHTML=`<div class="panel"><h2>Создать промокод</h2><form class="admin-form" id="promotion-form">
      <label>Код<input name="code" placeholder="PARK10" required minlength="3" maxlength="32"></label>
      <label>Скидка, %<input name="percent" type="number" min="1" max="90" value="10" required></label>
      <label>Лимит применений<input name="max_uses" type="number" min="1" max="100000" value="100" required></label>
      <label>Минимальная сумма, MDL<input name="minimum" type="number" min="0" max="24000" step="0.01" value="0" required></label>
      <label>Парковка<select name="lot_id">${lotOptions(lots,true)}</select></label>
      <label>Действует с<input name="starts_at" type="datetime-local"></label>
      <label>Действует до<input name="ends_at" type="datetime-local"></label>
      <button class="button" type="submit">Добавить промокод</button></form></div>
      <div class="panel table-wrap"><h2>Промокоды</h2><table class="data-table"><thead><tr><th>Код</th><th>Скидка</th><th>Применения</th><th>Условия</th><th>Статус</th><th></th></tr></thead><tbody>
      ${rows.map(p=>`<tr><td><strong>${escapeHtml(p.code)}</strong></td><td>${p.percent}%</td><td>${p.used} / ${p.max_uses}<small>В резерве: ${p.held}</small></td>
      <td>${escapeHtml(p.lot_id?lots.find(l=>l.id===p.lot_id)?.name || p.lot_id:'Все парковки')}<small>От ${escapeHtml(money(p.min_amount_cents))}${p.starts_at?' · с '+date(p.starts_at):''}${p.ends_at?' · до '+date(p.ends_at):''}</small></td>
      <td>${p.active?'Включён':'Выключен'}</td><td><button class="button secondary small" data-toggle-promo="${escapeHtml(p.code)}" data-active="${p.active}">${p.active?'Выключить':'Включить'}</button></td></tr>`).join('')}</tbody></table></div>`;
  $('#promotion-form').addEventListener('submit',e=>{
    e.preventDefault();
    busy(e.submitter,async()=>{
      const f=e.target.elements;
      await api('/promotions',{method:'POST',body:{code:f.code.value,percent:Number(f.percent.value),
        max_uses:Number(f.max_uses.value),min_amount_cents:Math.round(Number(f.minimum.value)*100),
        lot_id:f.lot_id.value || null,starts_at:f.starts_at.value?new Date(f.starts_at.value).toISOString():null,
        ends_at:f.ends_at.value?new Date(f.ends_at.value).toISOString():null}});
      await promotionsView();toast('Промокод добавлен');
    });
  });
}

async function maintenanceView() {
  const [rows,lots]=await Promise.all([api('/maintenance'),api('/parking/lots')]);
  if (state.view!=='maintenance') return;
  $('#content').innerHTML=`<div class="panel"><h2>Запланировать обслуживание</h2>
    <p class="muted">Выберите место и период. Если уже есть бронь, система предложит выбрать другое время.</p>
    <form class="admin-form" id="maintenance-form"><label>Парковка<select name="lot_id" required>${lotOptions(lots)}</select></label>
    <label>Место<select name="space_id" required></select></label>
    <label>Начало<input name="start_at" type="datetime-local" value="${escapeHtml(state.start)}" required></label>
    <label>Окончание<input name="end_at" type="datetime-local" value="${escapeHtml(state.end)}" required></label>
    <label>Причина<input name="reason" minlength="3" maxlength="200" placeholder="Например, ремонт покрытия" required></label>
    <button class="button" type="submit">Закрыть на этот период</button></form></div>
    <div class="panel table-wrap"><h2>Интервалы обслуживания</h2>
      ${rows.length?`<table class="data-table"><thead><tr><th>Парковка / место</th><th>Период</th><th>Причина</th><th>Статус</th><th></th></tr></thead><tbody>
      ${rows.map(m=>`<tr><td>${escapeHtml(m.lot_name||'Уточняется')}<small>${escapeHtml(m.space_code||m.space_id.slice(0,8))}</small></td>
        <td>${date(m.start_at)}<small>до ${date(m.end_at)}</small></td><td>${escapeHtml(m.reason)}</td>
        <td>${m.status==='PENDING'?'<span class="badge">Синхронизируется</span>':badge(m.status)}
        ${m.error?'<small>'+escapeHtml(m.error)+'</small>':''}</td>
        <td>${['ACTIVE','CANCELLING'].includes(m.status)?`<button class="button secondary small" data-cancel-maintenance="${escapeHtml(m.id)}">Открыть место</button>`:''}</td></tr>`).join('')}</tbody></table>`:'<p class="muted">Работы пока не запланированы.</p>'}</div>`;
  const form=$('#maintenance-form');
  async function populateSpaces(){
    const lot=form.elements.lot_id.value;
    form.elements.space_id.innerHTML='';
    if(!lot)return;
    const spaces=await api('/parking/lots/'+encodeURIComponent(lot)+'/spaces');
    if (form.elements.lot_id.value!==lot) return;
    form.elements.space_id.innerHTML=spaces.map(s=>`<option value="${escapeHtml(s.id)}">${escapeHtml(s.code)}</option>`).join('');
  }
  form.elements.lot_id.addEventListener('change',()=>populateSpaces().catch(alertError));
  form.addEventListener('submit',e=>{
    e.preventDefault();
    busy(e.submitter,async()=>{
      const f=form.elements;
      const body={space_id:f.space_id.value,start_at:new Date(f.start_at.value).toISOString(),
        end_at:new Date(f.end_at.value).toISOString(),reason:f.reason.value};
      const key=JSON.stringify(body);
      if(state.maintenanceIntent?.key!==key)state.maintenanceIntent={key,id:crypto.randomUUID()};
      await api('/maintenance',{method:'POST',body:{...body,idempotency_key:state.maintenanceIntent.id}});
      state.maintenanceIntent=null;
      await maintenanceView();toast('Обслуживание запланировано');
    });
  });
  await populateSpaces();
}

async function reportsView() {
  if (!state.reportRange) {
    const end=new Date();end.setHours(24,0,0,0);
    const start=new Date(end);start.setDate(start.getDate()-7);
    state.reportRange={start:localInput(start),end:localInput(end)};
  }
  const range=state.reportRange;
  const data=await api('/reports/summary?'+new URLSearchParams({start_at:new Date(range.start).toISOString(),end_at:new Date(range.end).toISOString()}));
  if (state.view!=='reports') return;
  $('#content').innerHTML=`<form class="panel admin-form" id="report-form"><label>С<input name="start_at" type="datetime-local" required value="${escapeHtml(range.start)}"></label>
    <label>До<input name="end_at" type="datetime-local" required value="${escapeHtml(range.end)}"></label>
    <button class="button" type="submit">Обновить отчёт</button></form>
    ${data.import_complete?'':'<p class="demo-note">Импортируются ранее сохранённые брони и платежи. Обновите отчёт через несколько секунд.</p>'}
    <div class="report-grid">${[['Создано броней',data.bookings],['Учебные оплаты',money(data.paid_cents)],
      ['Возвраты',money(data.refunded_cents)],['За вычетом возвратов',money(data.net_cents)]].map(([label,value])=>
      `<div class="panel report-card"><span class="muted">${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join('')}</div>
    <div class="panel table-wrap"><h2>По парковкам</h2>
      <p class="muted">Подтверждено: ${data.statuses.CONFIRMED||0} · Отменено: ${data.statuses.CANCELLED||0} · Ожидают оплаты: ${data.statuses.PENDING||0} · Истекло: ${data.statuses.EXPIRED||0}</p>
      ${data.by_lot.length?`<table class="data-table"><thead><tr><th>Парковка</th><th>Создано броней</th><th>Из них отменено</th><th>Подтверждённых часов в периоде</th></tr></thead><tbody>
      ${data.by_lot.map(r=>`<tr><td>${escapeHtml(r.lot_name)}</td><td>${r.bookings}</td><td>${r.cancelled}</td><td>${r.reserved_hours.toLocaleString('ru-RU')}</td></tr>`).join('')}</tbody></table>`:'<p class="muted">За выбранный период данных нет.</p>'}
      <p class="hint">Брони считаются по дате создания, часы — по интервалу парковки. Оплаты и возвраты учитываются по датам операций.</p></div>`;
  $('#report-form').addEventListener('submit',e=>{
    e.preventDefault();
    busy(e.submitter,async()=>{
      state.reportRange={start:e.target.elements.start_at.value,end:e.target.elements.end_at.value};
      await reportsView();
    });
  });
}

document.addEventListener('click',event=>{
  const b=event.target.closest('button');
  if(!b)return;
  if(b.id==='apply-promo')busy(b,applyPromotion);
  if(b.id==='join-waitlist')busy(b,joinWaitlist);
  if(b.dataset.waitOffer)busy(b,()=>openOffer(b.dataset.waitOffer));
  if(b.dataset.waitCancel)busy(b,async()=>{
    await api('/waitlist/'+encodeURIComponent(b.dataset.waitCancel)+'/cancel',{method:'POST'});
    await waitlistView();toast('Заявка закрыта');
  });
  if(b.dataset.togglePromo)busy(b,async()=>{
    await api('/promotions/'+encodeURIComponent(b.dataset.togglePromo),{method:'PATCH',body:{active:b.dataset.active!=='true'}});
    await promotionsView();
  });
  if(b.dataset.cancelMaintenance)busy(b,async()=>{
    await api('/maintenance/'+encodeURIComponent(b.dataset.cancelMaintenance)+'/cancel',{method:'POST'});
    await maintenanceView();toast('Интервал обслуживания отменён');
  });
});
