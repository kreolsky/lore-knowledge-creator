import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
// Render the real project primitives; the preview needs no account or backend.
import { Button } from '../src/components/ui/Button';
import { IconButton } from '../src/components/ui/IconButton';
import { FieldTextarea } from '../src/components/ui/FieldInput';
import { FieldCheckbox } from '../src/components/ui/FieldCheckbox';
import { families, storedHighlights } from './palettes.mjs';

const paths: Record<string, React.ReactNode> = {
  file: <><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6M8 13h8M8 17h6"/></>,
  book: <><path d="M12 5v16M3 3c4-1 7 0 9 2 2-2 5-3 9-2v16c-4-1-7 0-9 2-2-2-5-3-9-2Z"/></>,
  down: <path d="m6 9 6 6 6-6"/>, right: <path d="m9 6 6 6-6 6"/>, plus: <path d="M12 5v14M5 12h14"/>,
  list: <path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>,
  chat: <path d="M21 11a7 7 0 0 1-7 7H8l-5 3V6a3 3 0 0 1 3-3h12a3 3 0 0 1 3 3Z"/>,
  chats: <><path d="M16 11V5a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2v10l4-3h8a2 2 0 0 0 2-1Z"/><path d="M8 16h10l4 3V9a2 2 0 0 0-2-2"/></>,
  clip: <path d="m21 11-9 9a6 6 0 0 1-9-9L14 2a4 4 0 0 1 6 6L9 19a2 2 0 0 1-3-3L16 6"/>,
  archive: <><path d="M4 8v12h16V8M2 3h20v5H2ZM10 12h4"/></>,
  arrow: <path d="M18 6 6 18M6 8v10h10"/>,
  shield: <><path d="m12 3 9 4v6c0 5-9 9-9 9s-9-4-9-9V7Z"/><path d="m8 12 3 3 5-6"/></>,
  search: <><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/></>,
  clock: <><circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/></>,
  settings: <><path d="m9 3-1 3-3 1-2 4 2 2 0 3 4 3 3-1 3 1 4-3 0-3 2-2-2-4-3-1-1-3Z"/><circle cx="12" cy="11" r="3"/></>,
  moon: <path d="M20 15a9 9 0 0 1-11-11 9 9 0 1 0 11 11Z"/>,
  sun: <><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l2 2M17 17l2 2M5 19l2-2M17 7l2-2"/></>,
  user: <><circle cx="12" cy="8" r="4"/><path d="M5 21v-3a7 7 0 0 1 14 0v3"/></>,
  logout: <path d="M9 3H4v18h5M9 12h12m-5-5 5 5-5 5"/>,
  mic: <><rect x="9" y="2" width="6" height="13" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3"/></>,
  send: <path d="m22 2-7 20-4-9-9-4Zm0 0L11 13"/>,
};
const I = ({ name, size = 16 }: { name: string; size?: number }) => <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
const Icon = ({ name, label }: { name: string; label: string }) => <IconButton title={label} aria-label={label}><I name={name}/></IconButton>;
const Mark = ({ n, children }: { n: number; children: React.ReactNode }) => <span className="cm-color-swatch" style={{ backgroundColor: storedHighlights[n - 1] }}>{children}</span>;
function TreeRow({ label, depth = 0, active = false, expanded = false }: { label: string; depth?: number; active?: boolean; expanded?: boolean }) {
  return <div className={`doc-item${active ? ' active' : ''}`} style={{ paddingLeft: 4 + depth * 12 }}>
    <span className="tree-chevron">{!active && <I name={expanded ? 'down' : 'right'} size={12}/>}</span>
    <I name="file" size={14}/><span className="doc-label">{label}</span>{active && <span className="tree-actions"><I name="list" size={12}/><I name="plus" size={12}/></span>}
  </div>;
}

export function renderPreview() {
  return renderToStaticMarkup(<>
    <div className="studio">
      <header className="studio-heading"><div><div className="eyebrow">LORE · THEME STUDY <span>12 / 10 / 2026</span></div><h1>Варианты оформления</h1></div><div className="study-count">03 направления <span>/</span> 06 тем</div></header>
      <section className="controls" aria-label="Выбор оформления">
        <div className="families">{families.map(f => <button key={f.id} type="button" data-family={f.id} aria-pressed={f.id === 'porcelain'}><span className="family-number">{f.number}</span><span>{f.name}</span><span className="family-dots"><i style={{ background: f.light.accent }}/><i style={{ background: f.dark.bg }}/></span></button>)}</div>
        <div className="modes"><button type="button" data-mode="light" aria-pressed="true"><I name="sun"/>Светлая</button><button type="button" data-mode="dark" aria-pressed="false"><I name="moon"/>Тёмная</button></div>
        <button className="original-toggle" type="button" aria-pressed="false">Исходная тема</button>
      </section>
      <div className="variant-caption"><div><span id="variant-number">01 / LIGHT</span><h2 id="variant-name">Porcelain</h2><span id="variant-note">Светлый холст, прохладные серые панели, чёткая иерархия.</span></div><span className="preview-label">Предпросмотр · контент из референса</span></div>
      <main className="app-frame" aria-label="Предпросмотр интерфейса Lore">
        <header className="app-header"><div className="header-left"><div className="logo bg-accent text-white">L</div><div className="breadcrumb"><span className="crumbProjectName">s.zaigraev</span><span className="sep"> : </span><span className="crumbAncestor">Инфографика</span><span className="sep"> : </span><span className="crumbActive">Ивенты</span></div><span className="rec"><I name="mic" size={14}/> rec</span></div><div className="right-tabs"><span className="tab active-tab"><I name="chats"/></span>{['clip','chat','archive','arrow','shield'].map(n => <span className="tab" key={n}><I name={n}/></span>)}<span className="tab-spacer"/><span className="tab"><I name="search"/></span><span className="tab"><I name="settings"/></span></div></header>
        <div className="app-body">
          <aside className="rail" aria-label="Панель инструментов"><span className="rail-active"><I name="file"/></span><span><I name="list"/></span><div className="rail-spacer"/><span><I name="moon"/></span><span className="rail-lang">EN</span><span><I name="user"/></span><span><I name="settings"/></span><span><I name="logout"/></span></aside>
          <aside className="sidebar"><div className="sidebarDocs"><div className="doc-item doc-item-context"><I name="book"/><span>Project Context</span></div><TreeRow label="WPC" expanded/><TreeRow label="Покер" depth={1} expanded/><TreeRow label="Инфографика" depth={2} expanded/><TreeRow label="Ивенты" depth={3} active/><TreeRow label="Справочники и выкаты" depth={1}/><TreeRow label="Рабочий буфер" depth={1}/><TreeRow label="Agent System"/><TreeRow label="Справка Lore"/><div className="add-document"><Button variant="dashed"><I name="plus" size={14}/>Add document</Button></div></div></aside>
          <article className="document" aria-label="Документ Ивенты"><div className="cm-content">
            <section><h3>Super week</h3><ul><li><Mark n={1}>Турнир длится неделю, играйте когда вам удобно</Mark></li><li><Mark n={2}>Игра идет на турнирные фишки</Mark></li><li><Mark n={3}>20% лидеров делят банк, 200 лидеров получают монеты и титулы!</Mark></li><li><Mark n={4}>Особые правила: Быстрые столы омаха</Mark></li></ul></section>
            <section><h3>Weekly</h3><ul><li><Mark n={5}>Турнир длится неделю, играйте когда вам удобно</Mark></li><li>Игра идет на турнирные фишки</li><li>20% лидеров делят банк, 200 лидеров получают монеты и титулы!</li></ul><span className="editor-caret"/></section>
            <section><h3>City</h3><ul><li>Турнир проходит с дд.мм.гг до дд.мм.гг ЧЧ:ММ играйте когда вам удобно</li><li>Игра идет на специальные турнирные фишки</li><li>750 лидеров получают монеты и покербоксы, 75 лучших игроков получат уникальную награду</li></ul></section>
            <section><h3>Whell of Fortune</h3><ul><li>Быстрые Sit-n-Go турниры</li><li>Награду в турнире определяет Колесо Фортуны</li><li>Побеждайте и копите звезды чтобы получить уникальную награду</li></ul></section>
            <section><h3>Joker</h3><ul><li>Холдем с добавлением 2 (4) джокеров в колоду</li><li>Особая награда за сбор специальной комбинации — «покер»</li><li>Удвойте стек и выйдите из-за стола чтобы забрать награду</li></ul></section>
            <section><h3>Royal Hold ’Em</h3><ul><li>Игра на старших картах колоды</li></ul></section>
          </div></article>
          <aside className="chat-panel"><div className="chat-plaque"><span>Start a conversation by typing a message below.</span><I name="search" size={14}/><I name="clock" size={14}/></div><div className="chat-list">{[['Доступен ли инструмент генерации изображений', '2026-10-09 14:42'], ['Расскажи мне про свои базовые', '2026-10-09 14:39'], ['Знакомство с возможностями ассистента', '2026-10-06 17:39']].map(([text, date]) => <div className="pill plain" key={text}><div>{text}</div><div className="chat-date">{date}<span> | Проба пера</span></div></div>)}</div>
            <div className="composer"><div className="composer-top"><Button variant="ghost"><I name="file" size={14}/>Context (1 docs, 0 refs)</Button><Button variant="ghost"><I name="list" size={14}/>Default<I name="down" size={12}/></Button></div><label className="input-label" htmlFor="preview-message">Сообщение (поле предпросмотра)</label><FieldTextarea name="preview-message" highlight defaultValue="" placeholder="Type a message to start a new chat (⌘Enter to send)" className="preview-textarea w-full"/><div className="composer-bottom"><div className="composer-selectors"><Button variant="composer" size="sm">openai/luna<I name="down" size={12}/></Button><Button variant="composer" size="sm">default<I name="down" size={12}/></Button><FieldCheckbox checked={false} onChange={() => {}} label="Auto"/></div><div className="composer-send"><Icon name="mic" label="Микрофон — визуальный образец"/><Button variant="primary" disabled title="Визуальный образец отправки"><I name="send" size={14}/>⌘Enter</Button></div></div></div>
          </aside>
        </div>
      </main>
      <section className="palette-section" aria-label="Палитра выбранной темы"><div className="palette-header"><h3>Цвета интерфейса</h3><span>HEX · CSS tokens</span></div><div id="swatches" className="swatches"/><div className="variant-explanation"><p id="family-description">{families[0].description}</p><p>Квадратные углы · жёлтый контекст · пять цветов маркеров</p></div></section>
      <footer className="studio-footer"><span>Основа: CSS и UI-компоненты lore-knowledge-creator</span><a href="themes.css" download>Скачать CSS всех вариантов ↓</a><span className="local-notice">Локальный визуальный стенд · отправка сообщений отключена</span></footer>
    </div>
  </>);
}
