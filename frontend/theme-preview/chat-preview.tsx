import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { Button } from '../src/components/ui/Button';
import { IconButton } from '../src/components/ui/IconButton';
import { FieldTextarea } from '../src/components/ui/FieldInput';
import { FieldCheckbox } from '../src/components/ui/FieldCheckbox';
import { chatPalettes } from './chat-palettes.mjs';

const paths: Record<string, React.ReactNode> = {
  chats: <><path d="M16 11V5a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2v10l4-3h8a2 2 0 0 0 2-1Z"/><path d="M8 16h10l4 3V9a2 2 0 0 0-2-2"/></>,
  chat: <path d="M21 11a7 7 0 0 1-7 7H8l-5 3V6a3 3 0 0 1 3-3h12a3 3 0 0 1 3 3Z"/>,
  file: <><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6M8 13h8M8 17h6"/></>,
  context: <><rect x="4" y="3" width="16" height="18"/><path d="M8 17h8M12 6v7M9 9.5h6"/></>,
  prompt: <><path d="M8 5H4v11h4M8 3h12v18H8Z M11 7h6M11 11h6M11 15h4"/></>,
  clip: <path d="m21 11-9 9a6 6 0 0 1-9-9L14 2a4 4 0 0 1 6 6L9 19a2 2 0 0 1-3-3L16 6"/>,
  archive: <path d="M4 8v12h16V8M2 3h20v5H2ZM10 12h4"/>,
  arrow: <path d="M18 6 6 18M6 8v10h10"/>,
  back: <path d="M19 12H5m6-6-6 6 6 6"/>,
  shield: <><path d="m12 3 9 4v6c0 5-9 9-9 9s-9-4-9-9V7Z"/><path d="m8 12 3 3 5-6"/></>,
  search: <><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/></>,
  clock: <><circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/></>,
  settings: <><path d="m9 3-1 3-3 1-2 4 2 2 0 3 4 3 3-1 3 1 4-3 0-3 2-2-2-4-3-1-1-3Z"/><circle cx="12" cy="11" r="3"/></>,
  down: <path d="m6 9 6 6 6-6"/>, right: <path d="m9 6 6 6-6 6"/>,
  copy: <><rect x="8" y="8" width="12" height="13"/><path d="M16 8V3H3v13h5"/></>,
  download: <path d="M12 3v12m-5-5 5 5 5-5M3 15v6h18v-6"/>,
  trash: <path d="M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7M14 10v7"/>,
  mic: <><rect x="9" y="2" width="6" height="13" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3"/></>,
  send: <path d="m22 2-7 20-4-9-9-4Zm0 0L11 13"/>,
};
function I({ name, size = 16 }: { name: string; size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
}
function Tool({ name, label }: { name: string; label: string }) {
  return <IconButton title={`${label} · образец`} aria-label={`${label} · образец`} size="sm"><I name={name} size={14}/></IconButton>;
}

function ChatScreen({ empty, palette }: { empty: boolean; palette: string }) {
  const state = empty ? 'empty' : 'active';
  return <section className={`chat-example is-${state}`} aria-label={empty ? 'Пустой чат' : 'Открытый диалог'}>
    <div className="screen-caption"><span>{empty ? 'A / Пустой чат' : 'B / Открытый диалог'}</span><span>{empty ? 'Подсказка + ввод' : 'Действия + сообщение'}</span></div>
    <div className="chat-window">
      <div className="chat-tabs"><span className="chat-tab selected"><I name="chats"/></span>{['clip', 'chat', 'archive', 'arrow', 'shield'].map(name => <span key={name} className="chat-tab"><I name={name}/></span>)}<span className="tabs-gap"/><span className="chat-tab"><I name="search"/></span><span className="chat-tab"><I name="settings"/></span></div>
      {empty ? <div className="empty-header"><strong>Start a conversation by typing a message below.</strong><I name="search" size={14}/><I name="clock" size={14}/></div>
        : <div className="dialog-header"><Button variant="primary" size="sm" title="Новый чат · образец"><I name="back" size={13}/>New chat</Button><I name="file" size={14}/><strong>Погода в Москве завтра</strong><Tool name="download" label="Скачать"/><Tool name="trash" label="Удалить"/></div>}
      <div className="chat-history">
        {empty ? <div className="empty-history">No chats yet. Type a message below to start the first one</div> : <>
          <div className="user-message"><div className="message-meta">finger | 2026-10-12 17:43</div><div className="user-bubble">Какая завтра будет погода в Москве?</div><Tool name="copy" label="Копировать сообщение"/></div>
          <div className="assistant-message"><div className="sample-source"><span>1.</span><I name="file" size={12}/><span>Проба пера!</span></div><div className="tool-summary"><I name="file" size={14}/>called tools, searched the web, visited web pages<I name="right" size={12}/></div><p>Мастер, завтра, <strong>13 октября</strong>, в Москве ожидается облачная погода и небольшой дождь. Ночью <strong>+9…+11 °C</strong>, днём <strong>+12…+14 °C</strong>. Зонт лучше взять с собой.</p><p>Прогноз «Московской правды»</p><div className="message-actions"><Tool name="file" label="Создать документ"/><Tool name="copy" label="Копировать ответ"/></div></div>
        </>}
      </div>
      <div className="chat-composer"><div className="chat-context"><Button variant="ghost"><I name="context" size={14}/>Context (1 docs, 0 refs)</Button><Button variant="ghost"><I name="prompt" size={14}/>Default<I name="down" size={12}/></Button>{!empty && <span className="token-count">16.4k / 1050k · 2%</span>}</div>
        <label className="sample-input"><span className="sr-only">{empty ? 'Сообщение для нового чата' : 'Сообщение в диалоге'} · {palette}</span><FieldTextarea readOnly name={`${palette}-${state}`} placeholder={empty ? 'Type a message to start a new chat (⌘Enter to send)' : 'Type a message… (⌘Enter to send)'} className="w-full"/></label>
        <div className="chat-controls"><div className="chat-selectors"><Button variant="composer" size="sm">openai/luna<I name="down" size={12}/></Button><Button variant="composer" size="sm">default<I name="down" size={12}/></Button><FieldCheckbox checked={false} onChange={() => {}} label="Auto"/></div><div className="chat-send"><Tool name="mic" label="Микрофон"/><Button variant="primary" disabled={empty} title="Отправка недоступна в образце"><I name="send" size={14}/>⌘Enter</Button></div></div>
      </div>
    </div>
  </section>;
}

export function renderChatPreview() {
  return renderToStaticMarkup(<main className="chat-study">
    <header className="chat-study-heading"><div><div className="study-eyebrow">LORE / CHAT COLOUR STUDY</div><h1>Четыре цвета для чата</h1><p>Оба состояния из ваших скриншотов. Одинаковая компоновка — разные акценты.</p></div><a className="earlier-study" href="index.html">Светлые и тёмные темы ↗</a></header>
    <nav className="palette-picker" aria-label="Цвет чата">{chatPalettes.map(p => <button key={p.id} type="button" data-pick={p.id} aria-pressed={p.id === 'lavender'} aria-controls={`palette-${p.id}`}><i style={{ backgroundColor: p.accent }}/><span>{p.number}</span>{p.name}</button>)}<button type="button" data-pick="all" aria-pressed="false">Все варианты ↓</button></nav>
    <p className="study-hint">Цвет объединяет активную вкладку, подсказку, ввод, New chat и сообщение пользователя.</p>
    <div className="palette-views">{chatPalettes.map(p => <section key={p.id} id={`palette-${p.id}`} className="palette-view" data-chat-palette={p.id} aria-labelledby={`title-${p.id}`}>
      <div className="palette-intro"><div><span>{p.number} / DARK</span><h2 id={`title-${p.id}`}>{p.name}</h2></div><p>{p.note}</p></div>
      <div className="chat-pair"><ChatScreen empty palette={p.name}/><ChatScreen empty={false} palette={p.name}/></div>
      <div className="chat-swatches" aria-label={`Цвета: ${p.name}`}>{[
        ['Акцент / кнопки', p.accent], ['Текст кнопок', p.onAccent], ['Вкладка / подсказка', p.tint],
        ['Ввод нового чата', p.input], ['Сообщение', p.bubble], ['Текст подсказки', p.ink],
      ].map(([label, hex]) => <div className="chat-swatch" key={label}><i style={{ backgroundColor: hex }}/><div><span>{label}</span><code>{hex}</code></div></div>)}</div>
    </section>)}</div>
    <footer className="chat-study-footer"><p>Фон панели #262626 · основной текст #DADADA · ввод в диалоге #2E2E2E</p><p>Визуальные образцы на основе компонентов Lore. Текст — из референса, отправка сообщений недоступна.</p></footer>
  </main>);
}
