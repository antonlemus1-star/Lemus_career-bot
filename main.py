async def handle_search(chat_id: int, is_admin: bool):
    await send_telegram(chat_id, "🔍 Анализирую резюме и подбираю вакансии...")
    resume = get_active_resume(chat_id)
    
    # Теперь ИИ сам понимает уровень и специальность пользователя из резюме
    prompt = (
        "Проанализируй резюме и сформируй ОДНУ максимально точную поисковую фразу для hh.ru (2-4 слова без кавычек), "
        "которая соответствует уровню квалификации и специальности пользователя. "
        "Используй слова, которые работодатели указывают в заголовках вакансий для такого уровня (Junior, Middle, Senior, Manager и т.д.).\n\n" + resume[:4000]
    )
    
    query = await asyncio.to_thread(ai_generate, prompt)
    if not query or query.startswith("⚠"):
        # Если ИИ не справился, просим пользователя уточнить
        await send_telegram(chat_id, "⚠️ Не удалось автоматически подобрать запрос. Напиши, какую должность ты ищешь?", get_keyboard(is_admin))
        return
        
    query = query.strip().strip('"').strip()
    await send_telegram(chat_id, f"🔍 Ищу по запросу: *{query}*...", get_keyboard(is_admin))

    items = await hh_api_search(query)
    if not items:
        await send_telegram(chat_id, f"⚠️ Не удалось найти вакансии по запросу «{query}». Попробуй сформулировать запрос иначе.", get_keyboard(is_admin))
        return

    # Фильтруем те, что пользователь уже отклонил
    valid_items = [v for v in items if str(v["id"]) not in ignored_vacancies]

    await send_telegram(chat_id, f"🔥 Нашел позиций по запросу «{query}»: {len(valid_items)}. Вывожу лучшие:", get_keyboard(is_admin))
    
    for v in valid_items[:12]:
        vid = str(v["id"])
        name = v.get("name") or "Вакансия"
        comp = v.get("company") or "Компания"
        temp_vacancies[vid] = {"title": name, "employer": comp}
        
        markup = {
            "inline_keyboard": [
                [
                    {"text": "✍️ Сопроводительное письмо", "callback_data": f"gen_{vid}"},
                    {"text": "👎 Не релевантно", "callback_data": f"ignore_{vid}"}
                ]
            ]
        }
        await send_telegram(chat_id, f"🏢 *{comp}*\n💼 [{name}]({v.get('url')})", markup)
        await asyncio.sleep(0.2)