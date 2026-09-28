/**
 * Inline markdown formatting for table cells (bold, italic, strikethrough, code, inline math).
 */
import { LIGATURE_RULES } from '../ligature-plugin';
import { renderMathCached } from '../math-render';

const INLINE_RE = /(\*\*(.+?)\*\*|~~(.+?)~~|\*(.+?)\*)/g;

/** Replace typographic ligature patterns with Unicode glyphs in plain text. */
function applyLigatures(text: string): string {
  let result = '';
  let i = 0;
  while (i < text.length) {
    if (i > 0 && text[i - 1] === '\\') {
      result += text[i];
      i++;
      continue;
    }
    let matched = false;
    for (const rule of LIGATURE_RULES) {
      if (text.startsWith(rule.pattern, i)) {
        result += rule.glyph;
        i += rule.pattern.length;
        matched = true;
        break;
      }
    }
    if (!matched) {
      result += text[i];
      i++;
    }
  }
  return result;
}

function appendInlineFormatted(frag: DocumentFragment, text: string): void {
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  INLINE_RE.lastIndex = 0;

  while ((match = INLINE_RE.exec(text)) !== null) {
    if (match.index > lastIndex)
      frag.appendChild(document.createTextNode(applyLigatures(text.slice(lastIndex, match.index))));

    if (match[2] != null) {
      const el = document.createElement('strong');
      el.className = 'cm-strong';
      el.textContent = match[2];
      frag.appendChild(el);
    } else if (match[3] != null) {
      const el = document.createElement('s');
      el.className = 'cm-strikethrough';
      el.textContent = match[3];
      frag.appendChild(el);
    } else if (match[4] != null) {
      const el = document.createElement('em');
      el.className = 'cm-em';
      el.textContent = match[4];
      frag.appendChild(el);
    }

    lastIndex = match.index + match[0].length;
  }

  if (lastIndex < text.length)
    frag.appendChild(document.createTextNode(applyLigatures(text.slice(lastIndex))));
}

function findNextSpecial(text: string, from: number): number {
  const tick = text.indexOf('`', from);
  const dollar = text.indexOf('$', from);
  if (tick === -1) return dollar;
  if (dollar === -1) return tick;
  return Math.min(tick, dollar);
}

export function renderInlineMarkdown(text: string): DocumentFragment {
  const frag = document.createDocumentFragment();
  let pos = 0;

  while (pos < text.length) {
    if (text[pos] === '`') {
      let openLen = 0;
      while (pos + openLen < text.length && text[pos + openLen] === '`') openLen++;

      let closeStart = pos + openLen;
      let found = false;
      while (closeStart <= text.length - openLen) {
        if (text[closeStart] === '`') {
          let closeLen = 0;
          while (closeStart + closeLen < text.length && text[closeStart + closeLen] === '`') closeLen++;
          if (closeLen === openLen) {
            let content = text.slice(pos + openLen, closeStart);
            if (content.length >= 2 && content[0] === ' ' && content[content.length - 1] === ' ')
              content = content.slice(1, -1);
            const code = document.createElement('code');
            code.className = 'cm-inline-code';
            code.textContent = content;
            frag.appendChild(code);
            pos = closeStart + closeLen;
            found = true;
            break;
          }
          closeStart += closeLen;
        } else {
          closeStart++;
        }
      }
      if (!found) {
        appendInlineFormatted(frag, text.slice(pos, pos + openLen));
        pos += openLen;
      }
    } else if (text[pos] === '$' && text[pos + 1] !== '$') {
      const close = text.indexOf('$', pos + 1);
      if (close > pos + 1) {
        const latex = text.slice(pos + 1, close);
        const span = document.createElement('span');
        span.className = 'cm-math-inline';
        span.innerHTML = renderMathCached(latex, false);
        frag.appendChild(span);
        pos = close + 1;
      } else {
        appendInlineFormatted(frag, text.slice(pos, pos + 1));
        pos++;
      }
    } else {
      const nextSpecial = findNextSpecial(text, pos);
      const end = nextSpecial === -1 ? text.length : nextSpecial;
      appendInlineFormatted(frag, text.slice(pos, end));
      pos = end;
    }
  }

  return frag;
}
