"""Seed development database with test users, projects, and documents.

Run inside the backend container:
    python seed_dev.py
"""

import asyncio
import re
from uuid import uuid4

from documents.service import create_document
from password import hash_secret

from db import apply_schema, create_record, extract_id, get_db

USERS = [
    {"name": "red", "email": "red@lore.app", "password": "red", "role": "user"},
    {"name": "black", "email": "black@lore.app", "password": "black", "role": "user"},
    {"name": "green", "email": "green@lore.app", "password": "green", "role": "user"},
]

PROJECTS = [
    {
        "id": "f1652f51",
        "name": "Мир Эльдории",
        "owner": "red@lore.app",
        "members": [
            {"email": "admin@lore.app", "access_level": "full"},
            {"email": "black@lore.app", "access_level": "full"},
            {"email": "green@lore.app", "access_level": "readonly"},
        ],
        "documents": [
            {
                "title": "История мира",
                "content": (
                    "# История мира Эльдории\n\n"
                    "Эльдория — мир, рождённый из осколков Первого Сна. Когда Великий Ткач "
                    "расплёл свою паутину, каждая нить стала рекой, каждый узел — горой.\n\n"
                    "## Первая эпоха: Рассвет\n\n"
                    "В начале были только Стражи — существа из чистого света. Они бродили по "
                    "бесформенным равнинам, оставляя за собой тропы, которые позже стали "
                    "торговыми путями Серебряной Империи.\n\n"
                    "## Вторая эпоха: Расколотые Королевства\n\n"
                    "Когда Стражи ушли за Край, их наследие раскололось на три ветви:\n"
                    "- **Дом Огня** — кузнецы и воины севера\n"
                    "- **Дом Воды** — мореходы южного побережья\n"
                    "- **Дом Тени** — учёные и маги подземных городов\n\n"
                    "Каждый Дом хранил осколок Первого Сна, дающий власть над стихией.\n\n"
                    "## Третья эпоха: Объединение\n\n"
                    "Пророчица Аэлин собрала все три осколка и создала Серебряный Трон. "
                    "Так началась эра единого королевства, продлившаяся тысячу лет.\n"
                ),
                "children": [
                    {
                        "title": "Дом Огня",
                        "content": (
                            "# Дом Огня\n\n"
                            "Клан кузнецов и воинов, основавший свою цитадель в жерле потухшего "
                            "вулкана Кор'Тал. Их кузницы горят день и ночь, выплавляя "
                            "звёздную сталь — металл, способный резать магию.\n\n"
                            "## Иерархия\n\n"
                            "- **Повелитель Пламени** — глава Дома\n"
                            "- **Хранители Горна** — мастера-кузнецы\n"
                            "- **Искровые** — элитные воины\n"
                            "- **Угольщики** — ученики\n\n"
                            "## Традиции\n\n"
                            "Каждый член Дома проходит Испытание Жаром в возрасте шестнадцати лет. "
                            "Выжившие получают клеймо на правом предплечье — знак принадлежности.\n"
                        ),
                    },
                    {
                        "title": "Дом Воды",
                        "content": (
                            "# Дом Воды\n\n"
                            "Мореходы и навигаторы, живущие на архипелаге Лазурных Островов. "
                            "Их корабли, построенные из кораллового дерева, не тонут даже в шторм.\n\n"
                            "## Флот\n\n"
                            "Великий Флот Дома Воды насчитывает триста кораблей:\n"
                            "- 50 боевых галер класса «Левиафан»\n"
                            "- 200 торговых каравелл\n"
                            "- 50 разведывательных шлюпов\n\n"
                            "## Морское Право\n\n"
                            "Любой спор на море решается поединком капитанов. Проигравший "
                            "отдаёт свой корабль победителю.\n"
                        ),
                    },
                ],
            },
            {
                "title": "Персонажи",
                "content": (
                    "# Ключевые персонажи\n\n"
                    "## Аэлин Серебряная\n\n"
                    "Пророчица, объединившая три Дома. Владеет даром предвидения, "
                    "но каждое использование стирает часть её памяти.\n\n"
                    "- **Возраст**: неизвестен (выглядит на 30)\n"
                    "- **Артефакт**: Серебряный Венец\n"
                    "- **Мотивация**: предотвратить возвращение Пожирателей\n\n"
                    "## Торн Кузнец\n\n"
                    "Последний Повелитель Пламени. Потерял руку в битве при Кор'Тале, "
                    "заменив её протезом из звёздной стали.\n\n"
                    "- **Возраст**: 45\n"
                    "- **Оружие**: молот Пепельный Рассвет\n"
                    "- **Мотивация**: восстановить честь своего Дома\n"
                ),
            },
        ],
    },
    {
        "id": str(uuid4())[:8],
        "name": "Sci-Fi Chronicle",
        "owner": "red@lore.app",
        "members": [
            {"email": "admin@lore.app", "access_level": "full"},
            {"email": "black@lore.app", "access_level": "commentator"},
        ],
        "documents": [
            {
                "title": "Station Omega",
                "content": (
                    "# Station Omega\n\n"
                    "Orbital research station positioned at Lagrange point L2, "
                    "beyond Earth's shadow. Crew complement: 240 scientists and engineers.\n\n"
                    "## Deck Layout\n\n"
                    "- **Deck 1-3**: Command and navigation\n"
                    "- **Deck 4-8**: Research laboratories\n"
                    "- **Deck 9-12**: Crew quarters and recreation\n"
                    "- **Deck 13**: Restricted — AI core\n\n"
                    "## The Anomaly\n\n"
                    "On day 847, sensors detected a spatial fold 200km off the port bow. "
                    "Initial readings showed negative mass — theoretically impossible. "
                    "Dr. Chen's team began round-the-clock monitoring.\n"
                ),
            },
        ],
    },
    {
        "id": "adm-full-01",
        "name": "Архив Хранителей",
        "owner": "admin@lore.app",
        "members": [
            {"email": "red@lore.app", "access_level": "full"},
        ],
        "documents": [
            {
                "title": "Кодекс Хранителей",
                "content": (
                    "# Кодекс Хранителей\n\n"
                    "Хранители — орден, существующий с Первой эпохи. Их задача — "
                    "сберегать знания и артефакты, оставшиеся после ухода Стражей.\n\n"
                    "## Клятва вступления\n\n"
                    "> Я клянусь хранить то, что было доверено мне. "
                    "Не открывать запечатанного, не разрушать сохранённого, "
                    "не забывать записанного.\n\n"
                    "## Ранги\n\n"
                    "1. **Искатель** — новый член, проходящий обучение\n"
                    "2. **Летописец** — хранитель текстов и карт\n"
                    "3. **Страж Свода** — допущен к запечатанным артефактам\n"
                    "4. **Верховный Хранитель** — глава ордена\n"
                ),
            },
            {
                "title": "Каталог артефактов",
                "content": (
                    "# Каталог артефактов\n\n"
                    "## Серебряный Венец\n"
                    "- **Происхождение**: создан Аэлин из трёх осколков Первого Сна\n"
                    "- **Свойства**: усиливает дар предвидения, но стирает воспоминания\n"
                    "- **Местонахождение**: Серебряный Трон, центральный зал\n\n"
                    "## Молот Пепельный Рассвет\n"
                    "- **Происхождение**: выкован в кузницах Кор'Тала\n"
                    "- **Свойства**: звёздная сталь, разрушает магические щиты\n"
                    "- **Местонахождение**: у Торна Кузнеца\n\n"
                    "## Компас Глубин\n"
                    "- **Происхождение**: дар морского бога Таласса\n"
                    "- **Свойства**: указывает на ближайший источник магии\n"
                    "- **Местонахождение**: утерян при крушении флагмана\n"
                ),
            },
        ],
    },
    {
        "id": "adm-read-01",
        "name": "Библиотека Забытых",
        "owner": "red@lore.app",
        "members": [
            {"email": "admin@lore.app", "access_level": "readonly"},
            {"email": "black@lore.app", "access_level": "full"},
        ],
        "documents": [
            {
                "title": "Запретные тексты",
                "content": (
                    "# Запретные тексты\n\n"
                    "Эти свитки были обнаружены в руинах Дома Тени после Великого Обвала. "
                    "Содержание частично повреждено, но сохранившиеся фрагменты описывают "
                    "ритуалы, способные открыть врата между мирами.\n\n"
                    "## Фрагмент I: Призыв Бездны\n\n"
                    "```\n"
                    "Когда три луны встанут в ряд,\n"
                    "И тень поглотит свет,\n"
                    "Произнеси слова назад —\n"
                    "И бездна даст ответ.\n"
                    "```\n\n"
                    "## Фрагмент II: Цена знания\n\n"
                    "Каждое использование ритуала требует жертвы: маг отдаёт год своей "
                    "жизни за минуту контакта с иным миром. Летописи Дома Тени полны "
                    "записей о магах, состарившихся за одну ночь.\n"
                ),
            },
        ],
    },
    {
        "id": "adm-comm-01",
        "name": "Хроники Войны Осколков",
        "owner": "black@lore.app",
        "members": [
            {"email": "admin@lore.app", "access_level": "commentator"},
            {"email": "green@lore.app", "access_level": "readonly"},
        ],
        "documents": [
            {
                "title": "Хронология конфликта",
                "content": (
                    "# Война Осколков — хронология\n\n"
                    "## Год 1: Кража\n"
                    "Неизвестный похитил осколок Дома Воды. Подозрения пали на Дом Тени.\n\n"
                    "## Год 2: Первые столкновения\n"
                    "Флот Дома Воды блокировал торговые пути Дома Тени. "
                    "Дом Огня объявил нейтралитет.\n\n"
                    "## Год 3: Эскалация\n"
                    "Дом Тени применил запрещённую магию разлома. Три острова архипелага "
                    "ушли под воду. Погибло 12 000 человек.\n\n"
                    "## Год 4: Вмешательство\n"
                    "Аэлин нашла украденный осколок — он был спрятан не Домом Тени, "
                    "а группой отступников из самого Дома Воды. Война прекратилась, "
                    "но доверие между Домами было разрушено на поколения.\n"
                ),
            },
            {
                "title": "Потери и последствия",
                "content": (
                    "# Потери Войны Осколков\n\n"
                    "## Людские потери\n"
                    "- Дом Воды: ~18 000 (включая гражданских на затопленных островах)\n"
                    "- Дом Тени: ~7 000 (в основном воины и маги)\n"
                    "- Дом Огня: ~500 (добровольцы-наёмники)\n\n"
                    "## Территориальные изменения\n"
                    "Три южных острова утрачены навсегда. Подземные города "
                    "восточного крыла Дома Тени обрушены. Торговый путь через "
                    "Серебряный Пролив закрыт на 50 лет из-за магического загрязнения.\n\n"
                    "## Политические последствия\n"
                    "Война привела к созданию Совета Трёх — первого межфракционного "
                    "органа управления, предшественника Серебряного Трона.\n"
                ),
            },
        ],
    },
]


def _slugify(title: str) -> str:
    """Convert title to a filename-safe slug."""
    slug = re.sub(r'[^\w\s-]', '', title.lower().strip())
    slug = re.sub(r'[\s_]+', '-', slug)
    return f"{slug}.md"


async def main():
    await apply_schema()
    db = await get_db()

    # Create users
    user_map: dict[str, str] = {}  # email -> user_id
    for u in USERS:
        existing = await db.query(
            "SELECT id FROM users WHERE email = $email LIMIT 1",
            {"email": u["email"]},
        )
        if existing:
            uid = extract_id(existing[0]["id"])
            print(f"  [skip] User {u['email']} already exists ({uid})")
            user_map[u["email"]] = uid
            continue
        uid = str(uuid4())
        await create_record("users", uid, {
            "name": u["name"],
            "email": u["email"],
            "password_hash": hash_secret(u["password"]),
            "role": u["role"],
            "user_facts": "",
        })
        user_map[u["email"]] = uid
        print(f"  [+] User {u['email']} ({uid})")

    # Also map any existing users referenced as owners (e.g. admin)
    all_owners = {p["owner"] for p in PROJECTS}
    all_member_emails = {m["email"] for p in PROJECTS for m in p.get("members", [])}
    for email in (all_owners | all_member_emails) - set(user_map.keys()):
        rows = await db.query("SELECT id FROM users WHERE email = $email LIMIT 1", {"email": email})
        if rows:
            user_map[email] = extract_id(rows[0]["id"])
            print(f"  [map] {email} -> {user_map[email]}")

    # Create projects with documents
    for proj in PROJECTS:
        owner_uid = user_map[proj["owner"]]

        existing = await db.query(
            "SELECT id FROM projects WHERE name = $name AND owner_id = $uid LIMIT 1",
            {"name": proj["name"], "uid": owner_uid},
        )

        index_doc_id = str(uuid4())
        if existing:
            pid = extract_id(existing[0]["id"])
            print(f"  [exists] Project '{proj['name']}' ({pid})")
            # Check if index doc already exists
            idx_rows = await db.query(
                "SELECT id FROM documents WHERE project_id = $pid AND path = 'project_context.md' AND deleted_at IS NONE",
                {"pid": pid},
            )
            if idx_rows:
                index_doc_id = extract_id(idx_rows[0]["id"])
            else:
                await create_document(index_doc_id, {
                    "project_id": pid,
                    "parent_id": None,
                    "title": "project_context.md",
                    "content": "",
                    "path": "project_context.md",
                    "is_index": True,
                })
                await db.query(
                    "UPDATE type::record('projects', $pid) SET index_doc_id = $idx, project_context = ''",
                    {"idx": index_doc_id, "pid": pid},
                )
                print(f"    [+] Index doc for existing project '{proj['name']}'")
        else:
            pid = proj["id"]
            await create_record("projects", pid, {
                "name": proj["name"],
                "owner_id": owner_uid,
                "is_public": False,
                "project_context": "",
                "index_doc_id": index_doc_id,
            })
            await create_document(index_doc_id, {
                "project_id": pid,
                "parent_id": None,
                "title": "project_context.md",
                "content": "",
                "path": "project_context.md",
                "is_index": True,
            })
            print(f"  [+] Project '{proj['name']}' ({pid})")

        # Add members (skip if already exists)
        for m in proj.get("members", []):
            member_uid = user_map.get(m["email"])
            if not member_uid:
                continue
            try:
                mid = str(uuid4())
                await create_record("project_members", mid, {
                    "project_id": pid,
                    "user_id": member_uid,
                    "access_level": m["access_level"],
                })
                print(f"    [+] Member {m['email']} ({m['access_level']})")
            except RuntimeError:
                pass  # already exists (unique index)

        # Create documents (skip if already exists)
        for doc in proj.get("documents", []):
            try:
                doc_id = str(uuid4())
                await create_document(doc_id, {
                    "project_id": pid,
                    "title": doc["title"],
                    "content": doc["content"],
                    "path": _slugify(doc["title"]),
                })
                print(f"    [+] Document '{doc['title']}'")
            except RuntimeError:
                print(f"    [skip] Document '{doc['title']}' already exists")
                # Find existing doc_id for children
                rows = await db.query(
                    "SELECT id FROM documents WHERE project_id = $pid AND path = $path AND deleted_at IS NONE LIMIT 1",
                    {"pid": pid, "path": _slugify(doc["title"])},
                )
                doc_id = extract_id(rows[0]["id"]) if rows else None

            # Create child documents
            for child in doc.get("children", []):
                try:
                    child_id = str(uuid4())
                    data = {
                        "project_id": pid,
                        "title": child["title"],
                        "content": child["content"],
                        "path": _slugify(child["title"]),
                    }
                    if doc_id:
                        data["parent_id"] = doc_id
                    await create_document(child_id, data)
                    print(f"      [+] Child '{child['title']}'")
                except RuntimeError:
                    print(f"      [skip] Child '{child['title']}' already exists")

    print("\nDone! Dev database seeded.")


if __name__ == "__main__":
    asyncio.run(main())
