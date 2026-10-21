#!/usr/bin/env python3
"""Builds an exam-prep note from all lectures of a subject: cheat sheet, exam questions with answers, practice test.

Usage: exam.py <Предмет>
"""
import re
import sys

from process import VAULT, chat, ensure_ollama, fix_math, lecture_notes, log, notify, subject_index, title_of

SYSTEM = """Ты — опытный преподаватель, который помогает студенту подготовиться к экзамену.
Тебе дают конспекты всех лекций по предмету. Опирайся только на них, ничего не выдумывай.
Используй только учебный материал: не спрашивай про организационные вещи (коллоквиумы, ДЗ, даты)
и про темы, которые лишь анонсированы на будущее, но не разбирались.
Не пиши общий заголовок в начале ответа — сразу содержимое.
Пиши по-русски, в Markdown. Формулы — в LaTeX ($...$ или $$...$$)."""

MATERIAL_WORDS = 9000  # above this the lectures are condensed in batches first

CONDENSE = """Сожми эти конспекты лекций, сохранив ВСЕ определения, формулы, теоремы, правила и ключевые примеры.
Убери пересказ и повторы. Сохрани заголовки лекций.

{text}"""

TASKS = [
    ("Шпаргалка", """Составь шпаргалку по предмету: самое важное из всех лекций в максимально сжатом виде —
определения, формулы, теоремы, правила. Группируй по темам (### заголовки), без воды.

{text}"""),
    ("Вопросы к экзамену", """Составь {n_questions} экзаменационных вопросов по всему материалу (как билеты) с ответами. Вопросы не должны повторяться.
Строго в формате, для каждого вопроса:

### N. <вопрос>
> [!success]- Ответ
> <ответ на 3-8 строк; каждая строка начинается с «> »>

{text}"""),
    ("Пробный тест", """Составь пробный тест из {n_test} вопросов с 4 вариантами ответа (один верный) по всему материалу. Вопросы не должны повторяться.
Строго в формате, для каждого вопроса:

**N. <вопрос>**
- a) …
- b) …
- c) …
- d) …

> [!success]- Ответ
> <буква> — <короткое объяснение>

{text}"""),
]


def tidy(md: str) -> str:
    """The note supplies the ## section heading; demote whatever headings the model wrote under it."""
    md = re.sub(r"^#{1,2} #{2,3} ", "### ", md, flags=re.M)
    lines = md.strip().splitlines()
    if lines and re.match(r"#{1,2} ", lines[0]):
        lines = lines[1:]  # the model's own title, e.g. "# Шпаргалка по предмету"
    return re.sub(r"^#{1,2} ", "### ", "\n".join(lines).strip(), flags=re.M)


def section(text: str, name: str) -> str:
    m = re.search(rf"^## {name}\n(.*?)(?=^## |\Z)", text, flags=re.S | re.M)
    return m.group(1).strip() if m else ""


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    subject = sys.argv[1]
    notes = lecture_notes(VAULT / subject)
    if not notes:
        notify("Экзамен", f"В папке «{subject}» нет лекций")
        sys.exit(1)

    parts, deadlines = [], []
    for n in notes:
        text = n.read_text(encoding="utf-8")
        body = "\n\n".join(s for s in (section(text, "Кратко"), section(text, "Конспект"),
                                       section(text, "Ключевые понятия")) if s)
        body = re.sub(r"!\[\[[^\]]+\]\]\n?", "", body)  # photos mean nothing to the model
        parts.append(f"# {title_of(n)}\n\n{body}")
        org = section(text, "Организационное")
        if org:
            deadlines.append(f"**[[{n.stem}|{title_of(n)}]]**\n{org}")

    ensure_ollama()
    material = "\n\n".join(parts)
    if len(material.split()) > MATERIAL_WORDS:
        batches, cur = [], []
        for p in parts:
            if cur and len(" ".join(cur + [p]).split()) > MATERIAL_WORDS:
                batches.append(cur)
                cur = []
            cur.append(p)
        batches.append(cur)
        condensed = []
        for i, b in enumerate(batches, 1):
            log(f"condensing batch {i}/{len(batches)}")
            condensed.append(chat(CONDENSE.format(text="\n\n".join(b)), SYSTEM))
        material = "\n\n".join(condensed)

    # ~5 questions per lecture: two short lectures don't make 25 distinct exam questions
    sizes = {"n_questions": min(25, 5 * len(notes)), "n_test": min(15, max(5, 3 * len(notes)))}
    out = []
    for name, prompt in TASKS:
        log(f"{subject}: {name}")
        out.append(f"## {name}\n\n{tidy(fix_math(chat(prompt.format(text=material, **sizes), SYSTEM)))}")

    index = subject_index(subject)
    note = VAULT / subject / f"📝 Экзамен — {subject}.md"
    lectures = "\n".join(f"- [[{n.stem}|{title_of(n)}]]" for n in notes)
    note.write_text(
        f"---\ntags: [экзамен, {subject.lower().replace(' ', '-')}]\n---\n\n"
        f"# 📝 Подготовка к экзамену: {subject}\n\n"
        f"**Предмет:** [[{index.stem}]] · по {len(notes)} лекциям\n\n"
        + "\n\n".join(out)
        + ("\n\n## Задания и даты\n\n" + "\n\n".join(deadlines) if deadlines else "")
        + f"\n\n## Лекции\n{lectures}\n",
        encoding="utf-8",
    )
    log(f"saved {note}")
    notify("Подготовка к экзамену готова 📝", subject)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ERROR: {e}")
        notify("Экзамен: ошибка", str(e))
        sys.exit(1)
