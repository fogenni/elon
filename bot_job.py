import mysql.connector.pooling
import os
from dotenv import load_dotenv
from yookassa import Configuration
from telegram import CallbackQuery
from telegram.ext import CallbackQueryHandler
from telegram.ext import ApplicationBuilder
from telegram import KeyboardButton
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram import ReplyKeyboardMarkup
from telegram import Update
import re
from datetime import datetime, timedelta, date
from mysql.connector.errors import IntegrityError
import uuid
from yookassa import Payment
from uuid import uuid4
from dateutil.relativedelta import relativedelta
from contextlib import contextmanager
from telegram.ext import (
    CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters
)

TYPE_PATTERN = re.compile(r'^(daily|weekly|monthly)$', re.IGNORECASE)

load_dotenv()
Configuration.account_id = os.getenv("YOOKASSA_SHOP_ID")
Configuration.secret_key = os.getenv("YOOKASSA_SECRET_KEY")

dbconfig = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "default-db",
    "password": "SQD3Fl*ZUszy",
    "database": "cloud_database_1"
}

# Создаём пул соединений
cnxpool = mysql.connector.pooling.MySQLConnectionPool(
    pool_name="mypool",
    pool_size=32,  # Максимум 32 одновременных подключений
    **dbconfig
)


@contextmanager
def db_connect():
    """
    Контекстный менеджер для работы с БД:
    – даёт вам conn и cursor
    – автоматически коммитит, если всё ок, или делает rollback
    – закрывает cursor и возвращает conn в пул
    """
    conn = cnxpool.get_connection()
    cursor = conn.cursor()
    try:
        yield conn, cursor
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


# Главный пользователь (супер-админ)
ADMIN_IDS = [1728077528]

# Состояния для ConversationHandler
ADD_EMPLOYEE = 0
SET_TASK_DESCRIPTION = 10
SET_TASK_DEADLINE = 11
SET_TASK_EMPLOYEE = 12


BONUS_EMPLOYEE = 20
BONUS_AMOUNT = 21



def get_table(table_base, company_id):
    if company_id:
        return f"company_{company_id}_{table_base}"
    return table_base

def get_company_id(user_id):
    """
    Возвращает ID компании для заданного Telegram user_id.
    Сначала проверяет, является ли пользователь администратором;
    затем ищет его в таблицах сотрудников всех компаний.
    """
    with db_connect() as (conn, cursor):
        # 1. Если пользователь — админ, сразу возвращаем его company_id
        cursor.execute(
            "SELECT id FROM companies WHERE admin_id = %s",
            (user_id,)
        )
        row = cursor.fetchone()
        if row:
            return row[0]

        # 2. Иначе перебираем все компании и ищем в их таблицах employees
        cursor.execute("SELECT id FROM companies")
        for (cid,) in cursor.fetchall():
            emp_table = get_table("employees", cid)
            try:
                cursor.execute(
                    f"SELECT id FROM {emp_table} WHERE telegram_id = %s",
                    (user_id,)
                )
                if cursor.fetchone():
                    return cid
            except mysql.connector.Error:
                continue
    # Если не нашли ни в одном из случаев
    return None

def is_company_admin(user_id):
    """
    Проверяет, является ли пользователь администратором какой-либо компании.
    """
    with db_connect() as (conn, cursor):
        cursor.execute(
            "SELECT id FROM companies WHERE admin_id = %s",
            (user_id,)
        )
        return cursor.fetchone() is not None

def main_menu_keyboard(user_id=None):
    keyboard = [
        ["🏠 Старт","👤 Мой профиль"], #"📑 Посмотреть задачи" #"💸 Моя зарплата"
        ["Я пришел", "Я ушел"],
        ["Мои посещения", "Мои зарплаты"],
        ["📋 Мои чеклисты", "😤 Выполнить задачу"],
        ["📦 Мой сбор"],
        ["📦 Мой сбор за день", "📦 Мой сбор за месяц"],
    ]
    if is_company_admin(user_id):
        keyboard.append(["👑 Админ", "💳 Моя подписка"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def handle_admin_section(update: Update, context: ContextTypes.DEFAULT_TYPE):
    section = update.message.text.strip()
    context.user_data["admin_section"] = section  # сохраняем текущий раздел
    keyboard = admin_submenu_keyboard(section, user_id=update.effective_user.id)
    title = re.sub(r"^[^a-zA-Zа-яА-Я0-9]+", "", section).strip(":")
    await update.message.reply_text(f"🔧 Раздел: *{title}*", parse_mode="Markdown", reply_markup=keyboard)

def admin_submenu_keyboard(section, user_id=None):
    menus = {
        "📌 Управление сотрудниками": [
            ["❌ Уволить сотрудника"],
            ["👁 Список сотрудников"],
            ["⬅️ Назад"]
        ],
        "📝 Задачи": [
            ["➕ Добавить задачу", "👁 Посмотреть задачи"],
            ["✅ Отметить выполненной"],
            ["⬅️ Назад"]
        ],
        "✅ Чек-листы": [
            ["👁 Просмотр чек-листов", "✅ Выполнить чек-лист"],
            ["➕ Добавить задачу в чек-лист"],
            ["📊 Отчет по чек-листам"],
            ["⬅️ Назад"]
        ],
        "📊 Зарплаты и посещения": [
            ["📈 Отчёт по зарплате", "👁 Посещения сотрудника"],
            ["🛠 Оклад и норма"],
            ["⬅️ Назад"]
        ],
        "💰 Премии и штрафы": [
            ["🎁 Премия", "⚠️ Штраф"],
            ["⬅️ Назад"]
        ],
        "📦 Сборка": [
            ["📋 Отчёт по сборке", "📊 Средняя сборка"],
            ["➕ Новый товар", "➖ Удалить товар"],
            ["⬅️ Назад"]
        ],
        "⚙️ Система": [
            ["🆔 ID компании"],
            ["💳 Оплатить подписку", "🆔 Мой ID"],
            ["📬 Обратная связь", "⬅️ Назад"]
        ]
    }
    return ReplyKeyboardMarkup(menus.get(section, [["⬅️ Назад"]]), resize_keyboard=True)


async def handle_admin_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    last_section = context.user_data.get("admin_section")

    if last_section:
        await admin_commands(update, context)
        context.user_data.pop("admin_section", None)
    else:
        await update.message.reply_text("Выберите раздел:", reply_markup=main_menu_keyboard(update.effective_user.id))


async def admin_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_company_admin(user_id):
        await update.message.reply_text("❌ У вас нет доступа к панели администратора.")
        return

    await update.message.reply_text(
        "👑 *Панель администратора:*\nВыберите раздел:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([
            ["📌 Управление сотрудниками", "📝 Задачи"],
            ["✅ Чек-листы", "📊 Зарплаты и посещения"],
            ["💰 Премии и штрафы", "📦 Сборка"],
            ["⚙️ Система", "❓ Что может этот бот"], 
            ["⬅️ Назад"]
        ], resize_keyboard=True)
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        with db_connect() as (conn, cursor):
            cursor.execute(
                "SELECT id FROM companies WHERE admin_id = %s",
                (user_id,)
            )
            company = cursor.fetchone()
    except Exception as err:
        await update.message.reply_text(f"❌ Ошибка при проверке компании: {err}")
        return

    # Если компания есть и подписка активна — сразу входим
    if company and check_subscription(user_id):
        return await login(update, context)

    # Иначе показываем приветственное меню
    welcome_text = (
        "👋 Добро пожаловать в команду!\n\n"
        "Выберите один из вариантов:\n"
        "• *Регистрация сотрудника* — если вы хотите зарегистрироваться как сотрудник.\n"
        "• *Регистрация компании* — если вы хотите создать компанию и стать её администратором.\n"
        "• *Войти* — если вы уже зарегистрированы и хотите попасть в систему.\n"
    )
    registration_keyboard = ReplyKeyboardMarkup(
        [
            ["Регистрация сотрудника", "Регистрация компании"],
            ["Войти", "❓ Что может этот бот"]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await update.message.reply_text(
        welcome_text,
        reply_markup=registration_keyboard,
        parse_mode="Markdown"
    )
# Логируем клик "Старт"
    try:
        with db_connect() as (conn, cursor):
            cursor.execute(
                "INSERT INTO start_clicks (telegram_id) VALUES (%s)",
                (user_id,),
            )
    except mysql.connector.Error as err:
        # Если уж совсем не хочется прерывать работу бота,
        # можно просто залогировать ошибку и продолжить:
        print(f"Не удалось залогировать старт: {err}")



async def show_features(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    # логируем клик «Что может этот бот»
    try:
        with db_connect() as (conn, cursor):
            cursor.execute(
                "INSERT INTO features_clicks (telegram_id) VALUES (%s)",
                (user_id,),
            )
    except mysql.connector.Error as err:
        print(f"Не удалось залогировать просмотр возможностей: {err}")
    
    features_text = (
        "*🤖 Что умеет этот бот:*\n\n"
        "• 🏢 Регистрация компании и сотрудников\n"
        "• ⏰ Учёт посещений: «Я пришел» / «Я ушел»\n"
        "• 💰 Расчёт и просмотр зарплаты по нормам и переработкам\n"
        "• 📋 Постановка и выполнение задач с дедлайнами\n"
        "• ✅ Ежедневные, еженедельные и ежемесячные чек-листы\n"
        "• 📦 Учёт сборок товаров и отчёты (сегодня, за месяц, среднее)\n"
        "• 🎁 Начисление бонусов и назначение штрафов\n"
        "• 📊 Админ-панель: управление всем в одном месте\n"
        "Этот бот избавит вас от рутинных Excel-таблиц и даст полный контроль "
        "над штатным расписанием, задачами и оплатой труда прямо в Telegram."
    )
    # Отправляем текст возможностей
    await update.message.reply_text(features_text, parse_mode="Markdown")

    # Теперь вручную показываем главное меню, как в start()
    registration_keyboard = ReplyKeyboardMarkup(
        [
            ["Регистрация сотрудника", "Регистрация компании"],
            ["Войти", "❓ Что может этот бот"]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await update.message.reply_text(
        "👋 Выберите один из вариантов:",
        reply_markup=registration_keyboard
    )
    # Больше ничего не возвращаем



async def login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    # логируем клик «Войти»
    try:
        with db_connect() as (conn, cursor):
            cursor.execute(
                "INSERT INTO login_clicks (telegram_id) VALUES (%s)",
                (user_id,),
            )
    except mysql.connector.Error as err:
        print(f"Не удалось залогировать вход: {err}")

    if not check_subscription(user_id):
        await update.message.reply_text(
            "❌ Подписка не активна. Введите /pay_subscription для активации."
        )
        return
    # Определяем компанию пользователя
    company_id = get_company_id(user_id)
    if not company_id:
        await update.message.reply_text("❌ Компания не найдена.")
        return

    try:
        
        with db_connect() as (conn, cursor):
            emp_table = get_table("employees", company_id)

            # Проверяем, есть ли запись сотрудника
            cursor.execute(
                f"SELECT id FROM {emp_table} WHERE telegram_id = %s",
                (user_id,)
            )
            row = cursor.fetchone()

            # Проверяем, является ли пользователь админом
            cursor.execute(
                "SELECT id FROM companies WHERE admin_id = %s",
                (user_id,)
            )
            company = cursor.fetchone()

        if row or company:
            
            if row:
                context.user_data['emp_id'] = row[0]
            await update.message.reply_text(
                "✅ Вы успешно вошли в систему!",
                reply_markup=main_menu_keyboard(user_id)
            )
        else:
            await update.message.reply_text(
                "❌ Вы не зарегистрированы. Пожалуйста, используйте регистрацию."
            )

    except Exception as err:
        
        await update.message.reply_text(f"❌ Ошибка при входе: {err}")


async def register_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Начинает регистрацию новой компании:
    – проверяет, что пользователь ещё не админ ни одной компании,
    """
    user_id = update.effective_user.id
    try:
        with db_connect() as (conn, cursor):
            cursor.execute(
                "INSERT INTO company_register_clicks (telegram_id) VALUES (%s)",
                (user_id,),
            )
    except mysql.connector.Error as err:
        print(f"Не удалось залогировать регистрацию компании: {err}")

    try:
        with db_connect() as (conn, cursor):
            cursor.execute(
                "SELECT id FROM companies WHERE admin_id = %s",
                (user_id,)
            )
            if cursor.fetchone():
                await update.message.reply_text("❌ У вас уже есть зарегистрированная компания.")
                return ConversationHandler.END

        await update.message.reply_text("Введите название вашей компании:")
        return 1
    except Exception as err:
        await update.message.reply_text(f"❌ Ошибка базы данных: {err}")
        return ConversationHandler.END



async def save_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Регистрирует новую компанию, создаёт её таблицы, добавляет администратора 
    и активирует пробную подписку на 3 месяца.
    """
    user_id = update.effective_user.id
    company_name = update.message.text

    try:
        # 1) Вставляем компанию и получаем её ID
        with db_connect() as (conn, cursor):
            cursor.execute(
                "INSERT INTO companies (name, admin_id) VALUES (%s, %s)",
                (company_name, user_id)
            )
            company_id = cursor.lastrowid

        # 2) Создаём все таблицы для этой компании
        create_company_tables(company_id)

        # 3) Добавляем администратора как первого сотрудника
        with db_connect() as (conn, cursor):
            emp_table = get_table("employees", company_id)
            admin_name = update.effective_user.full_name
            cursor.execute(
                f"INSERT INTO {emp_table} (name, telegram_id) VALUES (%s, %s)",
                (admin_name, user_id)
            )
        # 4) Вставляем пробную подписку на 3 месяца
        start_date = datetime.today().date()
        end_date   = start_date + relativedelta(months=3)
        with db_connect() as (conn, cursor):
            cursor.execute(
                """
                INSERT INTO subscriptions
                  (company_id, start_date, end_date, amount, status)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (company_id, start_date, end_date, 0, 'active')
            )
        await update.message.reply_text(
            f"✅ Компания «{company_name}» зарегистрирована!\n"
            f"Пробная подписка активна до {end_date}.",
            reply_markup=ReplyKeyboardMarkup(
                [["Войти"]],
                resize_keyboard=True,
                one_time_keyboard=True
            )
        )
        return ConversationHandler.END

    except Exception as err:
        
        await update.message.reply_text(f"❌ Ошибка при сохранении компании: {err}")
        return ConversationHandler.END
    
async def show_company_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    with db_connect() as (conn, cursor):
        try:
            cursor.execute("SELECT id, name FROM companies WHERE admin_id = %s", (user_id,))
            company = cursor.fetchone()
            if not company:
                await update.message.reply_text("❌ Вы не являетесь администратором ни одной компании.")
                return
            company_id, company_name = company
            await update.message.reply_text(f"🆔 ID вашей компании \"{company_name}\": {company_id}")
        except mysql.connector.Error as err:
            await update.message.reply_text(f"❌ Ошибка базы данных: {err}")
    


async def check_long_shifts(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    threshold = now - timedelta(hours=8)

    
    with db_connect() as (conn, cursor):
        
        cursor.execute("SELECT id FROM companies")
        companies = cursor.fetchall()

        # 2) Для каждой компании проверяем открытые смены
        for (company_id,) in companies:
            attendance_table = get_table("attendance", company_id)
            emp_table = get_table("employees", company_id)

            cursor.execute(f"""
                SELECT a.employee_id, a.start_time, e.telegram_id
                FROM {attendance_table} a
                JOIN {emp_table} e ON a.employee_id = e.id
                WHERE a.end_time IS NULL
            """)
            rows = cursor.fetchall()

            
            for employee_id, start_time, telegram_id in rows:
                if start_time < threshold:
                    await context.bot.send_message(
                        chat_id=telegram_id,
                        text='⏰ Напоминание: нажмите кнопку "Я ушел", если вы завершили работу.'
                    )
    
    


async def show_reg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:       
        with db_connect() as (conn, cursor):
            cursor.execute("SELECT COUNT(*) FROM companies")
            (count,) = cursor.fetchone()        
        await update.message.reply_text(f"📋 В базе зарегистрировано компаний: {count}")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при запросе: {e}")


async def pay_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Запускает процесс оплаты подписки.
    """
    if update.message:
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
    elif update.callback_query:
        await update.callback_query.answer()
        user_id = update.callback_query.from_user.id
        chat_id = update.callback_query.message.chat.id
    else:
        return

    try:
        # Ищем компанию администратора
        with db_connect() as (conn, cursor):
            cursor.execute("SELECT id FROM companies WHERE admin_id = %s", (user_id,))
            row = cursor.fetchone()

        if not row:
            await context.bot.send_message(chat_id, "❌ Вы не зарегистрировали компанию.")
            return

        company_id = row[0]

        # Генерация платежа в YooKassa
        payment_id = str(uuid.uuid4())
        amount_rub = 290
        return_url = f"https://t.me/{context.bot.username}"

        payment = Payment.create({
            "amount": {
                "value": f"{amount_rub}.00",
                "currency": "RUB"
            },
            "confirmation": {
                "type": "redirect",
                "return_url": return_url
            },
            "capture": True,
            "description": f"Подписка компании {company_id}",
            "metadata": {
                "company_id": str(company_id),
                "admin_id": str(user_id)
            }
        }, uuid.uuid4())

        confirm_url = payment.confirmation.confirmation_url
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"💳 Оплата подписки на 30 дней — {amount_rub}₽.\n\n"
                f"Перейдите по ссылке для оплаты:\n{confirm_url}\n\n"
            ),
            disable_web_page_preview=True
        )

        context.chat_data["payment_id"] = payment.id
        context.chat_data["company_id"] = company_id

        # Очищаем старые задачи проверки этого же платежа
        existing_jobs = context.job_queue.get_jobs_by_name(f"check_{payment.id}")
        for job in existing_jobs:
            job.schedule_removal()

        # Ставим задачу на повторную проверку статуса платежа
        context.job_queue.run_repeating(
            check_payment_status_job,
            interval=20,
            first=30,
            data={
                "chat_id": chat_id,
                "payment_id": payment.id,
                "company_id": company_id,
                "retries": 0
            },
            name=f"check_{payment.id}"
        )

    except Exception as e:
        await context.bot.send_message(chat_id, f"❌ Ошибка создания платежа: {e}")



def check_subscription(user_id):
    """
    Проверяет, активную подписку юзера.
    """
    try:
        with db_connect() as (conn, cursor):
            # 1) Сначала проверим, является ли пользователь админом
            cursor.execute(
                "SELECT id FROM companies WHERE admin_id = %s", 
                (user_id,)
            )
            row = cursor.fetchone()
            if row:
                company_id = row[0]
            else:
                # 2) Ищем его как сотрудника в каждой компании
                cursor.execute("SELECT id FROM companies")
                all_companies = cursor.fetchall()
                company_id = None
                for (cid,) in all_companies:
                    emp_table = f"company_{cid}_employees"
                    try:
                        cursor.execute(
                            f"SELECT id FROM {emp_table} WHERE telegram_id = %s",
                            (user_id,)
                        )
                        if cursor.fetchone():
                            company_id = cid
                            break
                    except mysql.connector.Error:
                        # Таблицы может не быть — пропускаем
                        continue

                if not company_id:
                    return False

            # 3) Проверяем дату окончания последней активной подписки
            cursor.execute(
                """
                SELECT end_date
                FROM subscriptions
                WHERE company_id = %s AND status = 'active'
                ORDER BY end_date DESC
                LIMIT 1
                """,
                (company_id,)
            )
            sub = cursor.fetchone()
            if not sub or sub[0] < datetime.today().date():
                return False

            return True

    except mysql.connector.Error as err:
        print(f"Ошибка базы данных в check_subscription: {err}")
        return False


async def check_payment_status_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    chat_id = data.get("chat_id")
    payment_id = data.get("payment_id")
    company_id = data.get("company_id")
    retries = data.get("retries", 0)

    if retries >= 9:
        await context.bot.send_message(chat_id, "⏱ Проверка оплаты завершена. Оплата не подтверждена.")
        context.job.schedule_removal()
        return
    data["retries"] = retries + 1
    context.job.data = data

    try:
        payment = Payment.find_one(payment_id)
        if payment.status == "succeeded":
            today = datetime.today().date()
            
            with db_connect() as (conn, cursor):            
                cursor.execute(
                    """
                    SELECT end_date
                    FROM subscriptions
                    WHERE company_id = %s AND status = 'active'
                    ORDER BY end_date DESC
                    LIMIT 1
                    """,
                    (company_id,)
                )
                row = cursor.fetchone()

                if row and row[0] and row[0] > today:
                    start_date = row[0]
                else:
                    start_date = today

                # Продлеваем на 30 дней
                end_date = start_date + timedelta(days=30)
                amount = int(float(payment.amount.value))

                # Вставляем новую подписку
                cursor.execute(
                    """
                    INSERT INTO subscriptions 
                        (company_id, start_date, end_date, amount, status)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (company_id, start_date, end_date, amount, 'active')
                )
                

            await context.bot.send_message(chat_id, f"✅ Подписка успешно активирована до {end_date}!")
            context.job.schedule_removal()
        
    except Exception as e:
        await context.bot.send_message(chat_id, f"⚠️ Ошибка при проверке оплаты: {e}")


EMPLOYEE_REGISTRATION_COMPANY = 1

async def register_employee_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.full_name
    # Сохраняем имя в контексте (на будущее, если нужно)
    context.user_data['employee_name'] = name
    await update.message.reply_text("Введите ID компании, к которой вы хотите присоединиться:")
    return EMPLOYEE_REGISTRATION_COMPANY

async def register_employee_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Продолжает регистрацию сотрудника в указанную компанию (ID вводится пользователем).
    Состояние: EMPLOYEE_REGISTRATION_COMPANY
    """
    
    try:
        company_id = int(update.message.text)
    except ValueError:
        await update.message.reply_text(
            "⚠️ Введите корректный числовой ID компании."
        )
        return EMPLOYEE_REGISTRATION_COMPANY

    try:
        with db_connect() as (conn, cursor):
            
            cursor.execute("SELECT id, name FROM companies WHERE id = %s", (company_id,))
            company = cursor.fetchone()
            if not company:
                await update.message.reply_text(
                    "⚠️ Компания с указанным ID не найдена. Проверьте и попробуйте ещё раз."
                )
                return EMPLOYEE_REGISTRATION_COMPANY

            
            user_id = update.effective_user.id
            name = context.user_data.get('employee_name', update.effective_user.full_name)
            emp_table = get_table("employees", company_id)
            cursor.execute(
                f"INSERT INTO {emp_table} (name, telegram_id) VALUES (%s, %s)",
                (name, user_id)
            )
        
        await update.message.reply_text(
            f"✅ Вы успешно зарегистрированы в компании \"{company[1]}\"!",
            reply_markup=ReplyKeyboardMarkup([["Старт"]], resize_keyboard=True, one_time_keyboard=True)
        )
        return ConversationHandler.END

    except mysql.connector.Error as err:
        await update.message.reply_text(f"❌ Ошибка базы данных: {err}")
        return ConversationHandler.END
            
HANDLE_DEADLINE_BUTTON = 10001
HANDLE_EMPLOYEE_BUTTON = 10002
SET_TASK_DEADLINE = 11  
SET_TASK_EMPLOYEE = 12

async def add_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    company_id = get_company_id(update.effective_user.id)
    if not company_id:
        await update.message.reply_text("❌ У вас нет прав администратора.")
        return

    await update.message.reply_text("📝 Введите описание задачи:")
    return SET_TASK_DESCRIPTION

async def set_task_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['task_description'] = update.message.text

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 До конца дня", callback_data="deadline_today")],
        [InlineKeyboardButton("🗓 До конца недели", callback_data="deadline_week")],
        [InlineKeyboardButton("📆 До конца месяца", callback_data="deadline_month")],
        [InlineKeyboardButton("✏️ Своя дата", callback_data="deadline_custom")]
    ])

    await update.message.reply_text("📅 Выберите срок выполнения:", reply_markup=keyboard)
    return HANDLE_DEADLINE_BUTTON  

async def handle_custom_date_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_input = update.message.text.strip()
        deadline = datetime.strptime(user_input, "%d-%m-%Y").date()
        context.user_data['task_deadline'] = deadline.strftime("%Y-%m-%d")

        return await show_employee_buttons(update, context)

    except ValueError:
        await update.message.reply_text("⚠️ Неверный формат. Введите дату в формате *ДД-ММ-ГГГГ*.", parse_mode="Markdown")
        return SET_TASK_DEADLINE  

async def handle_deadline_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    today = date.today()

    if query.data == "deadline_today":
        deadline = today
    elif query.data == "deadline_week":
        deadline = today + timedelta(days=(6 - today.weekday()))
    elif query.data == "deadline_month":
        next_month = today.replace(day=28) + timedelta(days=4)
        deadline = next_month.replace(day=1) - timedelta(days=1)
    elif query.data == "deadline_custom":
        await query.message.reply_text("✏️ Введите дату вручную в формате DD-MM-YYYY:")
        return SET_TASK_DEADLINE
    else:
        await query.message.reply_text("⚠️ Неизвестный выбор.")
        return ConversationHandler.END

    context.user_data['task_deadline'] = deadline.strftime("%Y-%m-%d")

    
    return await show_employee_buttons(query, context)


async def show_employee_buttons(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    """
    Показывает кнопки выбора сотрудника для назначения задачи.
    """
    
    user_id = (
        update_or_query.from_user.id
        if isinstance(update_or_query, CallbackQuery)
        else update_or_query.effective_user.id
    )
    company_id = get_company_id(user_id)
    emp_table = get_table("employees", company_id)

    try:
        with db_connect() as (conn, cursor):
            cursor.execute(f"SELECT id, name FROM {emp_table}")
            employees = cursor.fetchall()
    except mysql.connector.Error as err:
        await update_or_query.message.reply_text(f"❌ Ошибка базы данных: {err}")
        return ConversationHandler.END

    if not employees:
        await update_or_query.message.reply_text("⚠️ Нет сотрудников.")
        return ConversationHandler.END

    # Строим Inline-кнопки
    buttons = [
        [InlineKeyboardButton(name, callback_data=f"task_emp_{emp_id}")]
        for emp_id, name in employees
    ]
    markup = InlineKeyboardMarkup(buttons)

    await update_or_query.message.reply_text(
        "👥 Выберите сотрудника:", reply_markup=markup
    )
    return HANDLE_EMPLOYEE_BUTTON

async def handle_employee_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает выбор сотрудника из InlineKeyboard и сохраняет задачу в БД.
    """
    query = update.callback_query
    await query.answer()

    if not query.data.startswith("task_emp_"):
        await query.message.reply_text("⚠️ Неверный выбор.")
        return ConversationHandler.END

    emp_id = int(query.data.split("_")[-1])
    description = context.user_data.get('task_description')
    deadline = context.user_data.get('task_deadline')
    company_id = get_company_id(query.from_user.id)
    tasks_table = get_table("tasks", company_id)

    try:
        with db_connect() as (conn, cursor):
            cursor.execute(
                f"INSERT INTO {tasks_table} (employee_id, description, deadline) VALUES (%s, %s, %s)",
                (emp_id, description, deadline)
            )
    except mysql.connector.Error as err:
        await query.message.reply_text(f"❌ Ошибка базы данных: {err}")
        return ConversationHandler.END

    await query.message.reply_text("✅ Задача успешно добавлена!")
    return ConversationHandler.END



async def select_employee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    try:
        employee_id = int(update.message.text)
    except ValueError:
        await update.message.reply_text("⚠️ Пожалуйста, введите корректный ID сотрудника.")
        return SET_TASK_EMPLOYEE

    description = context.user_data['task_description']
    deadline = context.user_data['task_deadline']
    company_id = get_company_id(update.effective_user.id)
    tasks_table = get_table("tasks", company_id)

    try:
        
        with db_connect() as (conn, cursor):
            cursor.execute(
                f"INSERT INTO {tasks_table} (employee_id, description, deadline) "
                "VALUES (%s, %s, %s)",
                (employee_id, description, deadline)
            )
    except mysql.connector.Error as err:
        await update.message.reply_text(f"❌ Ошибка базы данных: {err}")
        return ConversationHandler.END

    await update.message.reply_text("✅ Задача успешно добавлена!")
    return ConversationHandler.END

async def admin_view_employees(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Показывает список сотрудников компании, если вы — её администратор.
    """
    user_id = update.effective_user.id
    try:
        with db_connect() as (conn, cursor):
            # Проверяем, администратор ли пользователь
            cursor.execute("SELECT id FROM companies WHERE admin_id = %s", (user_id,))
            company = cursor.fetchone()
            if not company:
                await update.message.reply_text("❌ Вы не являетесь администратором компании.")
                return

            company_id = company[0]
            emp_table = get_table("employees", company_id)

            # Получаем всех сотрудников
            cursor.execute(f"SELECT id, name FROM {emp_table}")
            employees = cursor.fetchall()

        # После выхода из with курсор и соединение уже закрыты,
        # но данные в переменной `employees` у нас остались.
        if not employees:
            await update.message.reply_text("⚠️ В вашей компании нет сотрудников.")
            return

        text = "👥 Сотрудники вашей компании:\n"
        for emp_id, emp_name in employees:
            text += f"🆔 ID: {emp_id} | 👤 {emp_name}\n"

        await update.message.reply_text(text)

    except mysql.connector.Error as err:
        await update.message.reply_text(f"❌ Ошибка базы данных: {err}")



async def view_checklists(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Показывает список чек-листов и их статус для текущего сотрудника.
    """
    user_id = update.effective_user.id
    try:
        with db_connect() as (conn, cursor):
            company_id = get_company_id(user_id)
            emp_table = get_table("employees", company_id)
            checklists_table = get_table("checklists", company_id)
            completions_table = get_table("checklist_completions", company_id)

            cursor.execute(f"SELECT id FROM {emp_table} WHERE telegram_id = %s", (user_id,))
            emp = cursor.fetchone()
            if not emp:
                await update.message.reply_text("❌ Вас нет в системе.")
                return
            emp_id = emp[0]

            cursor.execute(f"""
                SELECT
                  c.id,
                  c.checklist_type,
                  c.description,
                  CASE
                    WHEN c.checklist_type = 'daily' THEN (
                      SELECT COUNT(*) FROM {completions_table} cc
                      WHERE cc.checklist_id = c.id AND cc.employee_id = %s AND cc.completion_date = CURDATE()
                    )
                    WHEN c.checklist_type = 'weekly' THEN (
                      SELECT COUNT(*) FROM {completions_table} cc
                      WHERE cc.checklist_id = c.id AND cc.employee_id = %s
                        AND YEARWEEK(cc.completion_date, 1) = YEARWEEK(CURDATE(), 1)
                    )
                    WHEN c.checklist_type = 'monthly' THEN (
                      SELECT COUNT(*) FROM {completions_table} cc
                      WHERE cc.checklist_id = c.id AND cc.employee_id = %s
                        AND YEAR(cc.completion_date) = YEAR(CURDATE())
                        AND MONTH(cc.completion_date) = MONTH(CURDATE())
                    )
                    ELSE 0
                  END AS is_completed
                FROM {checklists_table} c
                WHERE c.employee_id = %s
                ORDER BY c.checklist_type
            """, (emp_id, emp_id, emp_id, emp_id))
            tasks = cursor.fetchall()

        if not tasks:
            await update.message.reply_text("📭 *Чек-листы отсутствуют.*", parse_mode="Markdown")
            return

        for checklist_id, checklist_type, description, is_completed in tasks:
            emoji = {"daily": "📅", "weekly": "🗓", "monthly": "📆"}.get(checklist_type, "📝")
            status = "✅ Выполнено" if is_completed else "❌ Не выполнено"

            text = (
                f"{emoji} *{checklist_type.capitalize()}*\n"
                f"📌 _{description}_\n"
                f"🆔 *ID:* `{checklist_id}`\n"
                f"🎯 {status}"
            )

            keyboard = None
            if not is_completed:
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Выполнить", callback_data=f"complete_{checklist_id}")
                ]])

            await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

    except mysql.connector.Error as err:
        await update.message.reply_text(f"❌ Ошибка базы данных: {err}")

async def complete_checklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Отмечает чек-лист выполненным по его ID.
    Использование: /complete_checklist <ID>
    """
    # Проверяем аргументы
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "⚠️ Используйте команду:\n`/complete_checklist <ID>`",
            parse_mode="Markdown"
        )
        return

    checklist_id = int(context.args[0])
    user_id = update.effective_user.id

    try:
        with db_connect() as (conn, cursor):
            # Определяем компанию
            company_id = get_company_id(user_id)
            if not company_id:
                await update.message.reply_text("❌ Компания не найдена.")
                return

            emp_table = get_table("employees", company_id)
            checklists_table = get_table("checklists", company_id)
            completions_table = get_table("checklist_completions", company_id)

            # Проверяем, что сотрудник есть в системе
            cursor.execute(f"SELECT id FROM {emp_table} WHERE telegram_id = %s", (user_id,))
            emp = cursor.fetchone()
            if not emp:
                await update.message.reply_text("❌ Вас нет в системе.")
                return
            emp_id = emp[0]

            # Проверяем, что такой чек-лист существует и принадлежит сотруднику
            cursor.execute(
                f"SELECT id FROM {checklists_table} WHERE id = %s AND employee_id = %s",
                (checklist_id, emp_id)
            )
            if not cursor.fetchone():
                await update.message.reply_text("⚠️ Указанная задача не найдена или не принадлежит вам.")
                return

            # Проверяем, не отмечен ли уже сегодня
            today = datetime.now().date()
            cursor.execute(
                f"""SELECT id FROM {completions_table}
                    WHERE checklist_id = %s AND employee_id = %s AND completion_date = %s""",
                (checklist_id, emp_id, today)
            )
            if cursor.fetchone():
                await update.message.reply_text("✅ Эта задача уже отмечена как выполненная сегодня.")
                return

            # Вставляем запись о выполнении
            cursor.execute(
                f"""INSERT INTO {completions_table}
                    (checklist_id, employee_id, completion_date, completed)
                    VALUES (%s, %s, %s, TRUE)""",
                (checklist_id, emp_id, today)
            )
        await update.message.reply_text(
            f"🎉 ✅ Задача с ID {checklist_id} отмечена как выполненная!",
            parse_mode="Markdown"
        )

    except mysql.connector.Error as err:
        await update.message.reply_text(f"❌ Ошибка базы данных: {err}")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Произошла ошибка: {e}")

async def handle_complete_checklist_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if not data.startswith("complete_"):
        return

    checklist_id = int(data.replace("complete_", ""))
    user_id = query.from_user.id

    try:
        with db_connect() as (conn, cursor):
            company_id = get_company_id(user_id)
            emp_table = get_table("employees", company_id)
            checklists_table = get_table("checklists", company_id)
            completions_table = get_table("checklist_completions", company_id)

            cursor.execute(
                f"SELECT id FROM {emp_table} WHERE telegram_id = %s",
                (user_id,)
            )
            emp = cursor.fetchone()
            if not emp:
                await query.edit_message_text("❌ Вас нет в системе.")
                return
            emp_id = emp[0]

            cursor.execute(
                f"SELECT id FROM {checklists_table} WHERE id = %s AND employee_id = %s",
                (checklist_id, emp_id)
            )
            if not cursor.fetchone():
                await query.edit_message_text("⚠️ Задача не найдена или не принадлежит вам.")
                return

            today = datetime.now().date()
            cursor.execute(
                f"""SELECT id FROM {completions_table}
                   WHERE checklist_id = %s AND employee_id = %s AND completion_date = %s""",
                (checklist_id, emp_id, today)
            )
            if cursor.fetchone():
                await query.edit_message_text("✅ Эта задача уже отмечена как выполненная сегодня.")
                return

            cursor.execute(
                f"""INSERT INTO {completions_table}
                    (checklist_id, employee_id, completion_date, completed)
                    VALUES (%s, %s, %s, TRUE)""",
                (checklist_id, emp_id, today)
            )

        await query.edit_message_text(f"🎉 ✅ Задача с ID {checklist_id} отмечена как выполненная!")

    except mysql.connector.Error as err:
        await query.edit_message_text(f"❌ Ошибка базы данных: {err}")
    except Exception as e:
        await query.edit_message_text(f"⚠️ Произошла ошибка: {e}")





CHOOSE_EMPLOYEE_ID_FOR_CHECKLIST = 1001
CHOOSE_TYPE_FOR_CHECKLIST = 1002
ENTER_DESCRIPTION_FOR_CHECKLIST = 1003

async def start_add_checklist_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    company_id = get_company_id(update.effective_user.id)
    if not company_id:
        await update.message.reply_text("❌ У вас нет прав администратора.")
        return ConversationHandler.END

    try:
        with db_connect() as (conn, cursor):
            emp_table = get_table("employees", company_id)
            cursor.execute(f"SELECT id, name FROM {emp_table}")
            employees = cursor.fetchall()

        if not employees:
            await update.message.reply_text("⚠️ В вашей компании нет сотрудников.")
            return ConversationHandler.END

        text = "👥 Введите ID сотрудника для чек-листа:\n\n"
        for emp_id, emp_name in employees:
            text += f"🆔 {emp_id} — {emp_name}\n"

        await update.message.reply_text(
            text,
            reply_markup=ReplyKeyboardMarkup(
                [["⬅️ Назад"]],
                resize_keyboard=True,
                one_time_keyboard=True
            )
        )
        return CHOOSE_EMPLOYEE_ID_FOR_CHECKLIST

    except mysql.connector.Error as err:
        await update.message.reply_text(f"❌ Ошибка базы данных: {err}")
        return ConversationHandler.END

async def choose_checklist_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        emp_id = int(update.message.text)
    except ValueError:
        await update.message.reply_text("⚠️ Введите корректный ID сотрудника.")
        return CHOOSE_EMPLOYEE_ID_FOR_CHECKLIST

    context.user_data["checklist_employee_id"] = emp_id

    keyboard = ReplyKeyboardMarkup([
        ["daily", "weekly", "monthly"]
    ], resize_keyboard=True)
    await update.message.reply_text("📅 Выберите тип чек-листа:", reply_markup=keyboard)
    return CHOOSE_TYPE_FOR_CHECKLIST

async def enter_checklist_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    checklist_type = update.message.text.strip().lower()
    if checklist_type not in ["daily", "weekly", "monthly"]:
        await update.message.reply_text("⚠️ Выберите: daily, weekly или monthly.")
        return CHOOSE_TYPE_FOR_CHECKLIST

    context.user_data["checklist_type"] = checklist_type
    await update.message.reply_text("📝 Введите описание задачи:")
    return ENTER_DESCRIPTION_FOR_CHECKLIST


async def save_checklist_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    description = update.message.text.strip()
    emp_id = context.user_data["checklist_employee_id"]
    checklist_type = context.user_data["checklist_type"]
    company_id = get_company_id(update.effective_user.id)

    try:
        with db_connect() as (conn, cursor):
            table = get_table("checklists", company_id)
            cursor.execute(f"""
                INSERT INTO {table} (employee_id, checklist_type, description)
                VALUES (%s, %s, %s)
            """, (emp_id, checklist_type, description))

        await update.message.reply_text(
            "✅ Задача добавлена в чек-лист.",
            reply_markup=admin_submenu_keyboard("✅ Чек-листы")
        )
        return ConversationHandler.END

    except mysql.connector.Error as err:
        await update.message.reply_text(f"❌ Ошибка базы данных: {err}")
        return ConversationHandler.END
        
    
async def cancel_add_checklist_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❎ Добавление задачи отменено.",
        reply_markup=admin_submenu_keyboard("✅ Чек-листы", user_id=update.effective_user.id)
    )
    return ConversationHandler.END



async def admin_view_employees_checklists(update: Update, context: ContextTypes.DEFAULT_TYPE):
    company_id = get_company_id(update.effective_user.id)
    if not company_id:
        await update.message.reply_text("❌ У вас нет прав администратора.")
        return

    try:
        with db_connect() as (conn, cursor):
            emp_table = get_table("employees", company_id)
            cursor.execute(
                f"SELECT id, name FROM {emp_table} ORDER BY name"
            )
            employees = cursor.fetchall()

        if not employees:
            await update.message.reply_text("⚠️ Сотрудников в системе нет.")
            return

        text = "👥 *Список сотрудников:*\n\n"
        for emp_id, emp_name in employees:
            text += f"🆔 ID: `{emp_id}` | 👤 {emp_name}\n"
        text += (
            "\n📩 Чтобы просмотреть чек-листы сотрудника, используйте:\n"
            "`/view_employee_checklists <ID>`"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

    except mysql.connector.Error as err:
        await update.message.reply_text(f"❌ Ошибка базы данных: {err}")

async def admin_view_employee_checklists(update: Update, context: ContextTypes.DEFAULT_TYPE):
    company_id = get_company_id(update.effective_user.id)
    if not company_id:
        await update.message.reply_text("❌ У вас нет прав администратора.")
        return

    try:
        emp_id = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text(
            "⚠️ Используйте команду:\n/view_employee_checklists <ID>",
            parse_mode="Markdown"
        )
        return

    try:
        with db_connect() as (conn, cursor):
            emp_table = get_table("employees", company_id)
            checklists_table = get_table("checklists", company_id)
            completions_table = get_table("checklist_completions", company_id)

            cursor.execute(f"SELECT name FROM {emp_table} WHERE id = %s", (emp_id,))
            emp_row = cursor.fetchone()
            if not emp_row:
                await update.message.reply_text("❌ Сотрудник не найден в вашей компании.")
                return
            emp_name = emp_row[0]

            cursor.execute(f"""
                SELECT
                  c.id,
                  c.checklist_type,
                  c.description,
                  IF(cc.completed IS NOT NULL, '✅ Выполнено', '❌ Не выполнено') AS status
                FROM {checklists_table} c
                LEFT JOIN {completions_table} cc
                  ON c.id = cc.checklist_id
                  AND cc.completion_date = CURDATE()
                WHERE c.employee_id = %s
                ORDER BY c.checklist_type
            """, (emp_id,))
            checklists = cursor.fetchall()

        if not checklists:
            await update.message.reply_text("📭 У сотрудника нет чек-листов.")
            return

        text = f"📝 *Чек-листы сотрудника* {emp_name}:\n"
        for cid, ctype, desc, status in checklists:
            text += (
                f"\n───────\n"
                f"│ 🆔 `{cid}` | {ctype.capitalize()} │\n"
                f"🎯 Статус: {status}\n"
                f"\n"
                f"📌 Описание:\n"
                f"{desc}\n"
                f"───────\n"
            )
        await update.message.reply_text(text, parse_mode="Markdown")

    except mysql.connector.Error as err:
        await update.message.reply_text(f"❌ Ошибка базы данных: {err}")


async def cancel_add_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❎ Добавление задачи отменено.",
        reply_markup=admin_submenu_keyboard("📝 Задачи", user_id=update.effective_user.id)
    )
    return ConversationHandler.END

async def view_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    try:
        with db_connect() as (conn, cursor):
            company_id = get_company_id(user_id)
            if not company_id:
                await update.message.reply_text("❌ Компания не найдена.")
                return

            emp_table   = get_table("employees", company_id)
            tasks_table = get_table("tasks", company_id)

            if is_company_admin(user_id):
                cursor.execute(f"""
                    SELECT t.id, e.name, t.description, t.deadline, t.completed
                    FROM {tasks_table} t
                    JOIN {emp_table} e ON t.employee_id = e.id
                    ORDER BY t.completed ASC, t.deadline ASC
                """)
            else:
                # Для обычного сотрудника — только свои
                cursor.execute(
                    f"SELECT id FROM {emp_table} WHERE telegram_id = %s",
                    (user_id,)
                )
                row = cursor.fetchone()
                if not row:
                    await update.message.reply_text("❌ Вас нет в системе.")
                    return
                emp_id = row[0]

                cursor.execute(f"""
                    SELECT t.id, e.name, t.description, t.deadline, t.completed
                    FROM {tasks_table} t
                    JOIN {emp_table} e ON t.employee_id = e.id
                    WHERE e.id = %s
                    ORDER BY t.completed ASC, t.deadline ASC
                """, (emp_id,))

            tasks = cursor.fetchall()

        if not tasks:
            await update.message.reply_text("📭 Задачи отсутствуют.")
            return

        
        text = "📋 *Список задач:*\n\n"
        for tid, name, desc, deadline, completed in tasks:
            status = "✅ Выполнена" if completed else "❌ В процессе"
            text += (
                f"• *Задача* (ID: `{tid}`):\n"
                f"  • *Сотрудник:* {name}\n"
                f"  • *Дедлайн:* {deadline}\n"
                f"  • *Статус:* {status}\n"
                f"  • *Описание:*\n"
                f"    _{desc}_\n\n\n"
            )

        await update.message.reply_text(text, parse_mode="Markdown")

    except mysql.connector.Error as err:
        await update.message.reply_text(f"❌ Ошибка базы данных: {err}")

async def checklist_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    company_id = get_company_id(user_id)
    if not company_id:
        await update.message.reply_text("❌ У вас нет прав администратора.")
        return

    emp_table = get_table("employees", company_id)
    checklists_table = get_table("checklists", company_id)
    completions_table = get_table("checklist_completions", company_id)

    try:
        with db_connect() as (conn, cursor):
            cursor.execute(f"""
                SELECT e.name, c.id, c.checklist_type, c.description,
                  COALESCE((
                    SELECT MAX(cc.completion_date)
                    FROM {completions_table} cc
                    WHERE cc.checklist_id = c.id AND cc.employee_id = e.id
                  ), NULL) AS last_completed_date
                FROM {checklists_table} c
                JOIN {emp_table} e ON c.employee_id = e.id
                ORDER BY e.name, c.checklist_type
            """)
            rows = cursor.fetchall()

        if not rows:
            await update.message.reply_text("⚠️ В системе нет чек-листов.")
            return

        today = date.today()
        current_year, current_week = today.isocalendar()[0:2]

        report = "📋 *Отчёт по выполнению чек-листов:*\n\n"
        for name, checklist_id, checklist_type, description, last_completed in rows:
            if checklist_type == "daily":
                is_done = (last_completed == today)
            elif checklist_type == "weekly":
                is_done = (
                    last_completed
                    and date.fromisoformat(str(last_completed)).isocalendar()[1] == current_week
                )
            elif checklist_type == "monthly":
                is_done = (
                    last_completed
                    and last_completed.month == today.month
                    and last_completed.year == today.year
                )
            else:
                is_done = False

            status = "✅ Выполнено" if is_done else "❌ Не выполнено"
            last_str = last_completed.strftime("%Y-%m-%d") if last_completed else "—"
            report += (
                f"👤 *{name}*\n"
                f"🔸 Тип: {checklist_type} | 📌 {description}\n"
                f"🎯 {status} | 🗓 Последнее выполнение: {last_str}\n\n"
            )

        await update.message.reply_text(report, parse_mode="Markdown")

    except mysql.connector.Error as err:
        await update.message.reply_text(f"❌ Ошибка базы данных: {err}")


async def complete_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    company_id = get_company_id(user_id)
    if not company_id:
        await update.message.reply_text("❌ Компания не найдена.")
        return

    emp_table   = get_table("employees", company_id)
    tasks_table = get_table("tasks", company_id)

    try:
        with db_connect() as (conn, cursor):
            cursor.execute(f"SELECT id FROM {emp_table} WHERE telegram_id = %s", (user_id,))
            emp = cursor.fetchone()
            if not emp and not is_company_admin(user_id):
                await update.message.reply_text("❌ Вас нет в системе.")
                return

            # Берём все невыполненные задачи (для админа — всех, для сотрудника — только свои)
            if is_company_admin(user_id):
                cursor.execute(f"""
                    SELECT t.id, e.name, t.description, t.deadline, t.completed
                    FROM {tasks_table} t
                    JOIN {emp_table} e ON t.employee_id = e.id
                    WHERE t.completed = 0
                    ORDER BY t.deadline ASC
                """)
            else:
                emp_id = emp[0]
                cursor.execute(f"""
                    SELECT t.id, e.name, t.description, t.deadline, t.completed
                    FROM {tasks_table} t
                    JOIN {emp_table} e ON t.employee_id = e.id
                    WHERE t.completed = 0 AND t.employee_id = %s
                    ORDER BY t.deadline ASC
                """, (emp_id,))

            tasks = cursor.fetchall()

        if not tasks:
            await update.message.reply_text("✅ Нет невыполненных задач.")
            return

        text = "🔧 *Список невыполненных задач:*\n\n"
        for tid, name, desc, deadline, completed in tasks:
            text += (
                f"• *Задача* (ID: `{tid}`):\n"
                f"  • *Сотрудник:* {name}\n"
                f"  • *Дедлайн:* {deadline}\n"
                f"  • *Статус:* ❌ В процессе\n"
                f"  • *Описание:*\n"
                f"    _{desc}_\n\n\n"
            )

        text += "✏️ Введите *ID* задачи, которую хотите отметить как выполненной:"
        await update.message.reply_text(text, parse_mode="Markdown")

        # Запоминаем, что ждём ввода ID
        context.user_data['awaiting_task_completion'] = {'company_id': company_id}

    except mysql.connector.Error as err:
        await update.message.reply_text(f"❌ Ошибка базы данных: {err}")

async def confirm_task_completion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'awaiting_task_completion' not in context.user_data:
        return  

    try:
        task_id = int(update.message.text)
    except ValueError:
        await update.message.reply_text("⚠️ Введите корректный числовой ID задачи.")
        return

    company_id = context.user_data['awaiting_task_completion'].get("company_id")
    if not company_id:
        await update.message.reply_text("❌ Компания не определена.")
        return

    tasks_table = get_table("tasks", company_id)

    try:
        with db_connect() as (conn, cursor):
            cursor.execute(f"SELECT id FROM {tasks_table} WHERE id = %s", (task_id,))
            if not cursor.fetchone():
                await update.message.reply_text("⚠️ Задача с таким ID не найдена.")
                return

            cursor.execute(f"UPDATE {tasks_table} SET completed = 1 WHERE id = %s", (task_id,))

        await update.message.reply_text("✅ Задача успешно отмечена как выполненная!")
    except mysql.connector.Error as err:
        await update.message.reply_text(f"❌ Ошибка базы данных: {err}")
    finally:
        context.user_data.pop('awaiting_task_completion', None)

async def my_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    user_id = update.effective_user.id
    
    company_id = get_company_id(user_id)
    if not company_id:
        await update.message.reply_text("❌ Компания не найдена.")
        return

    emp_table = get_table("employees", company_id)
    bonuses_table = get_table("bonuses", company_id)
    attendance_table = get_table("attendance", company_id)

    try:
        with db_connect() as (conn, cursor):
            cursor.execute(f"""
                SELECT id, name, base_salary, work_hours_norm, overhour_rate, underhour_rate
                FROM {emp_table}
                WHERE telegram_id = %s
            """, (user_id,))
            row = cursor.fetchone()
            if not row:
                await update.message.reply_text("❌ Вас нет в системе. Обратитесь к администратору.")
                return

            emp_id, name, base_sal, norm_hours, over_rate, under_rate = row

            today = datetime.today()
            year, month = today.year, today.month

            cursor.execute(f"""
                SELECT IFNULL(SUM(duration_seconds), 0)
                FROM {attendance_table}
                WHERE employee_id = %s AND YEAR(start_time) = %s AND MONTH(start_time) = %s
            """, (emp_id, year, month))
            current_sec = cursor.fetchone()[0]

            cursor.execute(f"""
                SELECT IFNULL(SUM(bonus_amount), 0)
                FROM {bonuses_table}
                WHERE employee_id = %s AND YEAR(bonus_date) = %s AND MONTH(bonus_date) = %s
            """, (emp_id, year, month))
            bonus_total = cursor.fetchone()[0] or 0

            cursor.execute("""
                SELECT s.end_date
                FROM subscriptions s
                JOIN companies c ON c.id = s.company_id
                WHERE c.admin_id = %s AND s.status = 'active'
                ORDER BY s.end_date DESC
                LIMIT 1
            """, (user_id,))
            sub_row = cursor.fetchone()

        # 3) Приводим типы и рассчитываем
        current_hours = float(current_sec) / 3600.0  # <-- здесь приводим Decimal к float
        salary = base_sal
        if current_hours > norm_hours:
            salary += (current_hours - norm_hours) * over_rate
        elif current_hours < norm_hours:
            salary -= (norm_hours - current_hours) * under_rate

        salary += float(bonus_total)
        salary = int(salary)

        # 4) Формируем информацию по подписке
        if sub_row and sub_row[0]:
            end_date = sub_row[0]
            days_left = (end_date - date.today()).days
            sub_info = f"\n\n🗓 Подписка до: {end_date}\n⏳ Осталось дней: {days_left}"
        else:
            sub_info = "\n\n❌ Подписка неактивна."

        # 5) Отправляем результат пользователю
        text = (
            f"🧑 Сотрудник: {name}\n"
            f"💰 Зарплата за {year}-{month:02d}: {salary} ₽"
        )
        await update.message.reply_text(text + sub_info)

    except mysql.connector.Error as err:
        await update.message.reply_text(f"❌ Ошибка базы данных: {err}")
        



# 1) Новый коллбэк, который придёт через 8 часов
async def notify_if_not_ended(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    attendance_id = data['attendance_id']
    company_id    = data['company_id']
    telegram_id   = data['telegram_id']

 
    attendance_table = get_table("attendance", company_id)
    with db_connect() as (conn, cursor):
        cursor.execute(
            f"SELECT end_time FROM {attendance_table} WHERE id = %s",
            (attendance_id,)
        )
        row = cursor.fetchone()


    if row and row[0] is None:
        await context.bot.send_message(
            chat_id=telegram_id,
            text='⏰ Напоминание: нажмите кнопку "Я ушел", если вы завершили работу.'
        )



async def start_shift(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    company_id = get_company_id(user_id)
    if not company_id:
        await update.message.reply_text("❌ Компания не найдена.")
        return

    emp_table       = get_table("employees", company_id)
    attendance_table= get_table("attendance", company_id)

    try:
        with db_connect() as (conn, cursor):
            cursor.execute(
                f"SELECT id FROM {attendance_table} WHERE employee_id = %s AND end_time IS NULL",
                (context.user_data.get('emp_id', None),)
            )
            if cursor.fetchone():
                await update.message.reply_text("⚠️ Вы уже отметились как пришедший!")
                return

            now = datetime.now()
            cursor.execute(
                f"INSERT INTO {attendance_table} (employee_id, start_time) VALUES (%s, %s)",
                (context.user_data['emp_id'], now)
            )
            attendance_id = cursor.lastrowid

        context.job_queue.run_once(
            notify_if_not_ended,
            when=8 * 3600,
            name=f"shift_reminder_{attendance_id}",
            data={
                'attendance_id': attendance_id,
                'company_id': company_id,
                'telegram_id': user_id
            }
        )

        await update.message.reply_text("✅ Вы отметились как пришедший на работу!")

    except mysql.connector.Error as err:
        await update.message.reply_text(f"❌ Ошибка базы данных: {err}")


async def end_shift(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    company_id = get_company_id(user_id)
    if not company_id:
        await update.message.reply_text("❌ Компания не найдена.")
        return

    attendance_table = get_table("attendance", company_id)

    try:
        with db_connect() as (conn, cursor):
            cursor.execute(f"""
                SELECT id, start_time
                FROM {attendance_table}
                WHERE employee_id = %s AND end_time IS NULL
                ORDER BY id DESC
                LIMIT 1
            """, (context.user_data.get('emp_id', None),))
            shift = cursor.fetchone()
            if not shift:
                await update.message.reply_text("⚠️ У вас нет открытого рабочего периода.")
                return

            attendance_id, start_time = shift

            for job in context.job_queue.get_jobs_by_name(f"shift_reminder_{attendance_id}"):
                job.schedule_removal()

            now = datetime.now()
            duration_sec = int((now - start_time).total_seconds())
            cursor.execute(f"""
                UPDATE {attendance_table}
                SET end_time = %s, duration_seconds = %s
                WHERE id = %s
            """, (now, duration_sec, attendance_id))

        await update.message.reply_text(
            f"✅ Вы отметились как ушедший! Сессия: {duration_sec} с (~{duration_sec/3600:.2f} ч)."
        )

    except mysql.connector.Error as err:
        await update.message.reply_text(f"❌ Ошибка базы данных: {err}")

async def my_full_salary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    company_id = get_company_id(user_id)
    if not company_id:
        await update.message.reply_text("❌ Компания не найдена.")
        return

    emp_table = get_table("employees", company_id)
    attendance_table = get_table("attendance", company_id)
    bonuses_table = get_table("bonuses", company_id)

    try:
        with db_connect() as (conn, cursor):
            cursor.execute(f"""
                SELECT id, base_salary, work_hours_norm, overhour_rate, underhour_rate
                FROM {emp_table}
                WHERE telegram_id = %s
            """, (user_id,))
            row = cursor.fetchone()
            if not row:
                await update.message.reply_text("❌ Вас нет в системе.")
                return
            emp_id, base_sal, norm_hours, over_rate, under_rate = row

            cursor.execute(f"""
                SELECT IFNULL(SUM(duration_seconds), 0)
                FROM {attendance_table}
                WHERE employee_id = %s
            """, (emp_id,))
            total_sec = cursor.fetchone()[0]
            total_hours = total_sec / 3600.0

            cursor.execute(f"""
                SELECT IFNULL(SUM(bonus_amount), 0)
                FROM {bonuses_table}
                WHERE employee_id = %s
            """, (emp_id,))
            cumulative_bonus = float(cursor.fetchone()[0])

        if total_hours > norm_hours:
            final_salary = base_sal + (total_hours - norm_hours) * over_rate
        elif total_hours < norm_hours:
            final_salary = base_sal - (norm_hours - total_hours) * under_rate
        else:
            final_salary = base_sal

        final_salary += cumulative_bonus
        final_salary = int(final_salary)

        await update.message.reply_text(
            f"👤 Ваши отработанные часы: {total_hours:.2f}\n"
            f"Норма часов: {norm_hours} ч.\n"
            f"Оклад: {base_sal} руб.\n\n"
            f"💰 Итоговая зарплата: {final_salary} руб."
        )

    except mysql.connector.Error as err:
        await update.message.reply_text(f"❌ Ошибка базы данных: {err}")

async def my_visits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Показывает историю посещений (дата → часы) для текущего пользователя.
    """
    user_id = update.effective_user.id
    company_id = get_company_id(user_id)
    if not company_id:
        await update.message.reply_text("❌ Компания не найдена.")
        return

    emp_table = get_table("employees", company_id)
    attendance_table = get_table("attendance", company_id)

    try:
        with db_connect() as (conn, cursor):
            cursor.execute(
                f"SELECT id FROM {emp_table} WHERE telegram_id = %s",
                (user_id,)
            )
            row = cursor.fetchone()
            if not row:
                await update.message.reply_text("❌ Вас нет в системе.")
                return
            emp_id = row[0]

            cursor.execute(f"""
                SELECT DATE(start_time) AS visit_date,
                       SUM(duration_seconds) AS total_seconds
                FROM {attendance_table}
                WHERE employee_id = %s
                GROUP BY DATE(start_time)
                ORDER BY DATE(start_time) DESC
            """, (emp_id,))
            visits = cursor.fetchall()

    except mysql.connector.Error as err:
        await update.message.reply_text(f"❌ Ошибка базы данных: {err}")
        return

    if not visits:
        await update.message.reply_text("📭 У вас пока нет посещений.")
        return

    text = "📅 История ваших посещений:\n"
    for visit_date, total_seconds in visits:
        hours = float(total_seconds) / 3600.0
        text += f"{visit_date}: {hours:.2f} ч.\n"

    await update.message.reply_text(text)

async def my_salaries(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Показывает ежемесячную зарплату пользователя за все месяцы, в которых есть данные.
    """
    user_id = update.effective_user.id
    company_id = get_company_id(user_id)
    if not company_id:
        await update.message.reply_text("❌ Компания не найдена.")
        return

    emp_table = get_table("employees", company_id)
    attendance_table = get_table("attendance", company_id)
    bonuses_table = get_table("bonuses", company_id)

    try:
        with db_connect() as (conn, cursor):
            cursor.execute(
                f"""
                SELECT id, base_salary, work_hours_norm, overhour_rate, underhour_rate
                FROM {emp_table}
                WHERE telegram_id = %s
                """,
                (user_id,)
            )
            row = cursor.fetchone()
            if not row:
                await update.message.reply_text("❌ Вас нет в системе.")
                return
            emp_id, base_sal, norm_hours, over_rate, under_rate = row

            cursor.execute(
                f"""
                SELECT YEAR(start_time) AS yr,
                       MONTH(start_time) AS mth,
                       SUM(duration_seconds) AS total_sec
                FROM {attendance_table}
                WHERE employee_id = %s
                GROUP BY yr, mth
                ORDER BY yr DESC, mth DESC
                """,
                (emp_id,)
            )
            months = cursor.fetchall()

        if not months:
            await update.message.reply_text("📭 Нет данных о зарплате.")
            return

        report = ["📊 *Зарплаты по месяцам:*"]
        for yr, mth, total_sec in months:
            hours = float(total_sec) / 3600.0

            salary = base_sal
            if hours > norm_hours:
                salary += (hours - norm_hours) * over_rate
            elif hours < norm_hours:
                salary -= (norm_hours - hours) * under_rate

            with db_connect() as (conn2, cur2):
                cur2.execute(
                    f"""
                    SELECT IFNULL(SUM(bonus_amount), 0)
                    FROM {bonuses_table}
                    WHERE employee_id = %s
                      AND YEAR(bonus_date) = %s
                      AND MONTH(bonus_date) = %s
                    """,
                    (emp_id, yr, mth)
                )
                bonus_total = cur2.fetchone()[0] or 0

            salary += bonus_total
            report.append(f"{yr}-{mth:02d}: {hours:.2f} ч. → {int(salary)} руб.")

        await update.message.reply_text("\n".join(report), parse_mode="Markdown")

    except mysql.connector.Error as err:
        await update.message.reply_text(f"❌ Ошибка базы данных: {err}")

async def set_salary_and_hours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    args = context.args or []
    if len(args) != 3:
        await update.message.reply_text(
            "⚠️ Используйте команду:\n"
            "/set_salary_and_hours <ID сотрудника> <оклад> <норма часов>"
        )
        return

    try:
        employee_id     = int(args[0])
        base_salary     = int(args[1])
        work_hours_norm = float(args[2])
    except ValueError:
        await update.message.reply_text(
            "⚠️ Используйте без <>:\n"
            "/set_salary_and_hours <ID сотрудника> <оклад> <норма часов>"
        )
        return

    admin_id   = update.effective_user.id
    company_id = get_company_id(admin_id)
    if not company_id:
        await update.message.reply_text("❌ У вас нет прав администратора.")
        return

    try:
        with db_connect() as (conn, cursor):
            emp_table = get_table("employees", company_id)
            cursor.execute(
                f"""
                UPDATE {emp_table}
                   SET base_salary = %s,
                       work_hours_norm = %s
                 WHERE id = %s
                """,
                (base_salary, work_hours_norm, employee_id)
            )
        await update.message.reply_text(
            f"✅ Оклад ({base_salary}₽) и норма ({work_hours_norm} ч) "
            f"обновлено для сотрудника ID {employee_id}."
        )
    except mysql.connector.Error as err:
        await update.message.reply_text(f"❌ Ошибка базы данных: {err}")

async def show_my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text(f"🆔 Ваш Telegram ID: {user_id}")

async def salary_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    user_id = update.effective_user.id
    company_id = get_company_id(user_id)
    if not company_id:
        await update.message.reply_text("❌ Компания не найдена.")
        return

    emp_table       = get_table("employees", company_id)
    attendance_table= get_table("attendance", company_id)
    bonuses_table   = get_table("bonuses", company_id)

    today         = datetime.today()
    current_year  = today.year
    current_month = today.month

    try:
        report_text = "📊 *Отчет по зарплатам (текущий месяц):*\n\n"
        with db_connect() as (conn, cursor):
            cursor.execute(f"""
                SELECT id, name, base_salary, work_hours_norm, overhour_rate, underhour_rate
                FROM {emp_table}
            """)
            employees = cursor.fetchall()

            if not employees:
                await update.message.reply_text("⚠️ Нет в системе.")
                return

            for emp_id, name, base_salary, norm_hours, over_rate, under_rate in employees:
                cursor.execute(f"""
                    SELECT IFNULL(SUM(duration_seconds), 0)
                    FROM {attendance_table}
                    WHERE employee_id = %s
                      AND YEAR(start_time)  = %s
                      AND MONTH(start_time) = %s
                """, (emp_id, current_year, current_month))
                row = cursor.fetchone()
                total_sec = row[0] if row else 0

                total_hours = float(total_sec) / 3600.0

                if total_hours > norm_hours:
                    salary = base_salary + (total_hours - norm_hours) * over_rate
                elif total_hours < norm_hours:
                    salary = base_salary - (norm_hours - total_hours) * under_rate
                else:
                    salary = base_salary

                cursor.execute(f"""
                    SELECT IFNULL(SUM(bonus_amount), 0)
                    FROM {bonuses_table}
                    WHERE employee_id = %s
                      AND YEAR(bonus_date)  = %s
                      AND MONTH(bonus_date) = %s
                """, (emp_id, current_year, current_month))
                bonus_row = cursor.fetchone()
                bonus = float(bonus_row[0]) if bonus_row and bonus_row[0] is not None else 0

                salary += bonus

                report_text += (
                    f"👤 *ID:* {emp_id} | *Имя:* {name}\n"
                    f"🕒 Отработано: {total_hours:.2f} ч. | 💰 Зарплата: {int(salary)} руб.\n\n"
                )

        await update.message.reply_text(report_text, parse_mode="Markdown")

    except mysql.connector.Error as err:
        await update.message.reply_text(f"❌ Ошибка базы данных: {err}")

async def view_attendance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Показывает историю посещений указанного сотрудника.
    Использование: /view_attendance <ID сотрудника>
    """
    company_id = get_company_id(update.effective_user.id)
    if not company_id:
        await update.message.reply_text("❌ У вас нет прав администратора.")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "⚠️ Используйте:\n"
            "/view_attendance <ID сотрудника>"
        )
        return

    emp_id = int(context.args[0])

    try:
        with db_connect() as (conn, cursor):
            emp_table        = get_table("employees", company_id)
            attendance_table = get_table("attendance", company_id)

            cursor.execute(
                f"SELECT name FROM {emp_table} WHERE id = %s",
                (emp_id,)
            )
            row = cursor.fetchone()
            if not row:
                await update.message.reply_text(
                    "⚠️ Сотрудник с таким ID не найден в вашей компании."
                )
                return
            emp_name = row[0]

            cursor.execute(f"""
                SELECT DATE(start_time), SUM(duration_seconds)
                FROM {attendance_table}
                WHERE employee_id = %s
                GROUP BY DATE(start_time)
                ORDER BY DATE(start_time) DESC
            """, (emp_id,))
            records = cursor.fetchall()

    except mysql.connector.Error as err:
        await update.message.reply_text(f"❌ Ошибка БД: {err}")
        return

    if not records:
        await update.message.reply_text(f"📭 У сотрудника {emp_name} пока нет посещений.")
        return

    text = f"📅 Посещения сотрудника *{emp_name}* (ID: {emp_id}):\n\n"
    for day, seconds in records:
        hours = float(seconds) / 3600.0
        text += f"{day}: {hours:.2f} ч.\n"

    await update.message.reply_text(text, parse_mode="Markdown")


async def delete_employee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    company_id = get_company_id(update.effective_user.id)
    if not company_id:
        await update.message.reply_text("❌ У вас нет прав администратора.")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "⚠️ Просто поставить метку - уволен :\n"
            "/delete_employee <ID сотрудника>\n"
            "\n"
            " ❌Полностью стереть со всех таблиц:\n"
            "/purge_employee <ID сотрудника>"
        )
        return

    emp_id = int(context.args[0])

    try:
        with db_connect() as (conn, cursor):
            emp_table = get_table("employees", company_id)

            cursor.execute(
                f"SELECT name FROM {emp_table} WHERE id = %s",
                (emp_id,)
            )
            row = cursor.fetchone()
            if not row:
                await update.message.reply_text("⚠️ Сотрудник с таким ID не найден.")
                return
            emp_name = row[0]

            if "Уволен" in emp_name:
                await update.message.reply_text("ℹ️ Сотрудник уже уволен.")
                return

            new_name = emp_name + " (Уволен)"
            cursor.execute(
                f"UPDATE {emp_table} SET name = %s WHERE id = %s",
                (new_name, emp_id)
            )

        await update.message.reply_text(f"✅ Сотрудник {emp_name} успешно уволен.")
    except mysql.connector.Error as err:
        await update.message.reply_text(f"❌ Ошибка БД: {err}")


async def award_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
 
    if not context.args or len(context.args) != 2:
        await update.message.reply_text(
            "⚠️ Используйте команду:\n"
            "/award_bonus <ID сотрудника> <сумма премии>"
        )
        return

    company_id = get_company_id(update.effective_user.id)
    if not company_id:
        await update.message.reply_text("❌ У вас нет прав администратора.")
        return

    try:
        emp_id = int(context.args[0])
        bonus_amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text("⚠️ Оба аргумента должны быть числами.")
        return

    today = datetime.today().date()

    try:
        with db_connect() as (conn, cursor):
            emp_table = get_table("employees", company_id)
            cursor.execute(
                f"SELECT id FROM {emp_table} WHERE id = %s",
                (emp_id,)
            )
            if not cursor.fetchone():
                await update.message.reply_text(f"⚠️ Сотрудник с ID {emp_id} не найден.")
                return

            bonuses_table = get_table("bonuses", company_id)
            cursor.execute(
                f"""
                INSERT INTO {bonuses_table}
                  (employee_id, bonus_amount, bonus_date)
                VALUES (%s, %s, %s)
                """,
                (emp_id, bonus_amount, today)
            )

        await update.message.reply_text(
            f"✅ Премия {bonus_amount} руб. успешно начислена сотруднику {emp_id}."
        )

    except mysql.connector.Error as err:
        await update.message.reply_text(f"❌ Ошибка БД: {err}")


async def assign_penalty(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not context.args or len(context.args) != 2:
        await update.message.reply_text(
            "⚠️ Используйте команду:\n"
            "/assign_penalty <ID сотрудника> <сумма штрафа>"
        )
        return

 
    company_id = get_company_id(update.effective_user.id)
    if not company_id:
        await update.message.reply_text("❌ У вас нет прав администратора.")
        return

    
    try:
        emp_id = int(context.args[0])
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text("⚠️ Оба аргумента должны быть числами.")
        return

    today = datetime.today().date()

    try:
        with db_connect() as (conn, cursor):
            emp_table = get_table("employees", company_id)
            cursor.execute(
                f"SELECT id FROM {emp_table} WHERE id = %s",
                (emp_id,)
            )
            if not cursor.fetchone():
                await update.message.reply_text(f"⚠️ Сотрудник с ID {emp_id} не найден.")
                return

            bonuses_table = get_table("bonuses", company_id)
            cursor.execute(
                f"""
                INSERT INTO {bonuses_table}
                  (employee_id, bonus_amount, bonus_date)
                VALUES (%s, %s, %s)
                """,
                (emp_id, -abs(amount), today)
            )

        await update.message.reply_text(
            f"✅ Штраф в размере {amount} руб. успешно назначен сотруднику {emp_id}."
        )

    except mysql.connector.Error as err:
        await update.message.reply_text(f"❌ Ошибка БД: {err}")


CHOOSE_TYPE    = 20
ENTER_QUANTITY = 21
CONFIRM_MORE   = 22


def ensure_collection_items_table_exists(company_id):
    """
    Убеждается, что таблица типов сборов для компании существует.
    Если нет — создаёт её.
    """
    table_name = get_table("collection_items", company_id)
    with db_connect() as (conn, cursor):
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(100) NOT NULL UNIQUE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)

def get_dynamic_collection_keyboard(company_id):
    """
    Возвращает клавиатуру с типами сборов для данной компании.
    Автоматически создаёт таблицу типов, если её нет.
    """
    ensure_collection_items_table_exists(company_id)  

   
    with db_connect() as (conn, cursor):
        table = get_table("collection_items", company_id)
        cursor.execute(f"SELECT name FROM {table}")
        items = [row[0] for row in cursor.fetchall()]

   
    buttons = [KeyboardButton(item) for item in items]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([KeyboardButton("⬅️ Главное меню")])

    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)



async def start_collection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    company_id = get_company_id(user_id)
    if not company_id:
        await update.message.reply_text("❌ Компания не найдена.")
        return ConversationHandler.END

    emp_table = get_table("employees", company_id)

 
    with db_connect() as (conn, cursor):
        cursor.execute(
            f"SELECT id FROM {emp_table} WHERE telegram_id = %s",
            (user_id,)
        )
        row = cursor.fetchone()

    if not row:
        await update.message.reply_text("❌ Вас нет в системе.")
        return ConversationHandler.END

    emp_id = row[0]
    context.user_data['emp_id'] = emp_id
    context.user_data['company_id'] = company_id

    await update.message.reply_text(
        "📦 Что вы собрали? Выберите тип:",
        reply_markup=get_dynamic_collection_keyboard(company_id)
    )
    return CHOOSE_TYPE


async def choose_collection_type(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.message.text == "⬅️ Главное меню":
        return await confirm_more(update, context)


    context.user_data['collection_type'] = update.message.text
    await update.message.reply_text("📝 Сколько штук собрать? Введите число:")
    return ENTER_QUANTITY
    


async def enter_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        qty = int(update.message.text)
    except ValueError:
        await update.message.reply_text("⚠️ Введите целое число.")
        return ENTER_QUANTITY  

    emp_id = context.user_data.get('emp_id')
    user_id = update.effective_user.id
    company_id = get_company_id(user_id)
    if not emp_id or not company_id:
        await update.message.reply_text("❌ Вас нет в системе.")
        return ConversationHandler.END

    item = context.user_data['collection_type']
    table = get_table("collections", company_id)

  
    with db_connect() as (conn, cursor):
        cursor.execute(f"""
            INSERT INTO {table}
              (employee_id, collection_date, item_type, quantity)
            VALUES (%s, CURDATE(), %s, %s)
        """, (emp_id, item, qty))

    await update.message.reply_text(
        f"✅ Записано: {qty} × {item}.\nДобавить ещё?",
        reply_markup=ReplyKeyboardMarkup([["Да", "Нет"]], resize_keyboard=True)
    )
    return CONFIRM_MORE


async def confirm_more(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "Да":
        return await start_collection(update, context)
    await update.message.reply_text(
        "Возвращаю в главное меню.",
        reply_markup=main_menu_keyboard(update.effective_user.id)
    )
    return ConversationHandler.END



async def my_collections_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    company_id = get_company_id(user_id)
    if not company_id:
        await update.message.reply_text("❌ Компания не найдена.")
        return

    with db_connect() as (conn, cursor):
        emp_table = get_table("employees", company_id)
        cursor.execute(f"SELECT id FROM {emp_table} WHERE telegram_id = %s", (user_id,))
        row = cursor.fetchone()
        if not row:
            await update.message.reply_text(
                "❌ Вы не зарегистрированы."
            )
            return
        emp_id = row[0]

        # 3) Собираем данные за сегодня
        coll_table = get_table("collections", company_id)
        cursor.execute(f"""
            SELECT item_type, SUM(quantity)
            FROM {coll_table}
            WHERE employee_id = %s AND collection_date = CURDATE()
            GROUP BY item_type
        """, (emp_id,))
        rows = cursor.fetchall()

    if not rows:
        await update.message.reply_text("📭 Сегодня ничего не записано.")
        return

    text = "📅 Сегодня вы собрали:\n"
    for item, total in rows:
        text += f"• {item}: {total}\n"
    await update.message.reply_text(text)


async def my_collections_avg(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id
    company_id = get_company_id(user_id)
    if not company_id:
        await update.message.reply_text("❌ Компания не найдена.")
        return

    with db_connect() as (conn, cursor):
        emp_table = get_table("employees", company_id)
        cursor.execute(f"SELECT id FROM {emp_table} WHERE telegram_id = %s", (user_id,))
        row = cursor.fetchone()
        if not row:
            await update.message.reply_text(
                "❌ Вы не зарегистрированы."
            )
            return
        emp_id = row[0]

        coll_table = get_table("collections", company_id)
        cursor.execute(f"""
            SELECT
              item_type,
              SUM(quantity)    AS total,
              COUNT(DISTINCT collection_date) AS days
            FROM {coll_table}
            WHERE employee_id = %s
              AND YEAR(collection_date)  = YEAR(CURDATE())
              AND MONTH(collection_date) = MONTH(CURDATE())
            GROUP BY item_type
        """, (emp_id,))
        results = cursor.fetchall()

    if not results:
        await update.message.reply_text("📭 В этом месяце нет данных о сборах.")
        return

    text = "📊 Средняя сборка за текущий месяц:\n"
    for item, total, days in results:
        average = total / days if days else 0
        text += f"• {item}: {total} шт. за {days} дн. → среднее: {average:.2f} шт./день\n"
    await update.message.reply_text(text)

def create_company_tables(company_id):
    
    with db_connect() as (conn, cursor):
        # Таблица сотрудников
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS company_{company_id}_employees (
                id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                name TEXT NOT NULL,
                telegram_id BIGINT UNIQUE,
                salary DOUBLE DEFAULT 0,
                base_salary INT DEFAULT 60000,
                work_hours_norm INT DEFAULT 40,
                overhour_rate INT DEFAULT 500,
                underhour_rate INT DEFAULT 500
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)

        # Таблица задач
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS company_{company_id}_tasks (
                id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                employee_id BIGINT UNSIGNED,
                description TEXT NOT NULL,
                deadline DATE,
                completed TINYINT(1) DEFAULT 0,
                FOREIGN KEY (employee_id)
                  REFERENCES company_{company_id}_employees(id)
                  ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)

        # Таблица посещаемости
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS company_{company_id}_attendance (
                id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                employee_id BIGINT UNSIGNED,
                start_time DATETIME NOT NULL,
                end_time DATETIME,
                duration_seconds INT DEFAULT 0,
                FOREIGN KEY (employee_id)
                  REFERENCES company_{company_id}_employees(id)
                  ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)

        # Таблица бонусов
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS company_{company_id}_bonuses (
                id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                employee_id BIGINT UNSIGNED,
                bonus_amount INT NOT NULL,
                bonus_date DATE NOT NULL,
                FOREIGN KEY (employee_id)
                  REFERENCES company_{company_id}_employees(id)
                  ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)

        # Таблица сборов
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS company_{company_id}_collections (
                id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                employee_id BIGINT UNSIGNED,
                collection_date DATE NOT NULL,
                item_type VARCHAR(50) NOT NULL,
                quantity INT NOT NULL,
                FOREIGN KEY (employee_id)
                  REFERENCES company_{company_id}_employees(id)
                  ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)

        # Таблица настраиваемых типов товаров для сбора
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS company_{company_id}_collection_items (
                id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(100) NOT NULL UNIQUE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)

        # Таблица чек-листов
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS company_{company_id}_checklists (
                id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                employee_id BIGINT UNSIGNED,
                checklist_type ENUM('daily','weekly','monthly') NOT NULL,
                description VARCHAR(255) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (employee_id)
                  REFERENCES company_{company_id}_employees(id)
                  ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)

        # Таблица выполненных чек-листов
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS company_{company_id}_checklist_completions (
                id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                checklist_id BIGINT UNSIGNED,
                employee_id BIGINT UNSIGNED,
                completion_date DATE NOT NULL,
                completed TINYINT(1) DEFAULT 0,
                FOREIGN KEY (checklist_id)
                  REFERENCES company_{company_id}_checklists(id)
                  ON DELETE CASCADE,
                FOREIGN KEY (employee_id)
                  REFERENCES company_{company_id}_employees(id)
                  ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)


async def my_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "💼 *Вы попали на страницу оплаты подписки.*\n\n"
        "🔐 *Платные функции:*\n"
        "• учёт зарплат\n"
        "• посещения\n"
        "• сборка\n"
        "• задачи и чек-листы\n\n"
        "💳 Подписка: *290₽ на 30 дней*.\n\n"
        "Нажмите кнопку ниже для оплаты."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Оплатить подписку", callback_data="start_payment")]
    ])
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query: CallbackQuery = update.callback_query
    await query.answer()
    print("handle_callback вызван с:", query.data)

    if query.data == "start_payment":
        await pay_subscription(update, context)


async def collections_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        company_id = get_company_id(user_id)
        if not company_id:
            await update.message.reply_text("❌ Компания не найдена.")
            return

        emp_table = get_table("employees", company_id)
        collections_table = get_table("collections", company_id)

        report = "📦 *Отчёт по сбору товаров:*\n\n"
        with db_connect() as (conn, cursor):
            cursor.execute(f"SELECT id, name FROM {emp_table} ORDER BY name")
            employees = cursor.fetchall()
            if not employees:
                await update.message.reply_text("⚠️ В компании нет сотрудников.")
                return

            for emp_id, name in employees:
                cursor.execute(f"""
                    SELECT DATE(collection_date), SUM(quantity)
                    FROM {collections_table}
                    WHERE employee_id = %s
                      AND MONTH(collection_date) = MONTH(CURDATE())
                      AND YEAR(collection_date) = YEAR(CURDATE())
                    GROUP BY DATE(collection_date)
                    ORDER BY DATE(collection_date)
                """, (emp_id,))
                days = cursor.fetchall()
                if not days:
                    continue

                report += f"👤 {name}\n"
                for d, amount in days:
                    report += f"   📅 {d}: {amount} шт.\n"
                report += "\n"

        if report.strip() == "📦 *Отчёт по сбору товаров:*":
            report += "\n❌ Нет данных о сборах."

        await update.message.reply_text(report, parse_mode="Markdown")

    except mysql.connector.Error as err:
        await update.message.reply_text(f"❌ Ошибка базы данных: {err}")


async def collections_avg_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        company_id = get_company_id(user_id)
        if not company_id:
            await update.message.reply_text("❌ Компания не найдена.")
            return

        emp_table = get_table("employees", company_id)
        collections_table = get_table("collections", company_id)

        report = "📊 *Средняя сборка за месяц по всем сотрудникам:*\n\n"
        with db_connect() as (conn, cursor):
            cursor.execute(f"SELECT id, name FROM {emp_table}")
            employees = cursor.fetchall()
            if not employees:
                await update.message.reply_text("⚠️ Нет сотрудников.")
                return

            for emp_id, name in employees:
                cursor.execute(f"""
                    SELECT item_type, SUM(quantity) AS total, COUNT(DISTINCT collection_date) AS days
                    FROM {collections_table}
                    WHERE employee_id = %s
                      AND MONTH(collection_date) = MONTH(CURDATE())
                      AND YEAR(collection_date) = YEAR(CURDATE())
                    GROUP BY item_type
                """, (emp_id,))
                items = cursor.fetchall()
                if not items:
                    continue

                report += f"👤 *{name}*\n"
                for item, total, days in items:
                    avg = total / days if days else 0
                    report += f"• {item}: {total} шт. за {days} дн. → среднее: {avg:.2f} шт./день\n"
                report += "\n"

        await update.message.reply_text(report, parse_mode="Markdown")

    except mysql.connector.Error as err:
        await update.message.reply_text(f"❌ Ошибка базы данных: {err}")



async def add_item_type(update: Update, context: ContextTypes.DEFAULT_TYPE):

    company_id = get_company_id(update.effective_user.id)
    if not company_id:
        await update.message.reply_text("❌ У вас нет прав администратора.")
        return

    if not context.args:
        await update.message.reply_text("⚠️ Используйте: /add_item_type <название товара>")
        return

    name = " ".join(context.args).strip()
    if not name:
        await update.message.reply_text("⚠️ Название не может быть пустым.")
        return

    try:
        table = get_table("collection_items", company_id)
        with db_connect() as (conn, cursor):
            cursor.execute(
                f"INSERT IGNORE INTO {table} (name) VALUES (%s)",
                (name,)
            )
        await update.message.reply_text(f"✅ Добавлен новый тип сбора: {name}")
    except mysql.connector.Error as err:
        await update.message.reply_text(f"❌ Ошибка базы данных: {err}")


async def delete_item_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    company_id = get_company_id(update.effective_user.id)
    if not company_id:
        await update.message.reply_text("❌ У вас нет прав администратора.")
        return

    if not context.args:
        await update.message.reply_text("⚠️ Используйте: /delete_item_type <название товара>")
        return

    name = " ".join(context.args).strip()
    if not name:
        await update.message.reply_text("⚠️ Название не может быть пустым.")
        return

    table = get_table("collection_items", company_id)
    try:
        with db_connect() as (conn, cursor):
            cursor.execute(f"DELETE FROM {table} WHERE name = %s", (name,))
            if cursor.rowcount:
                await update.message.reply_text(f"✅ Тип сборки «{name}» удалён.")
            else:
                await update.message.reply_text(f"⚠️ Тип «{name}» не найден.")
    except mysql.connector.Error as err:
        await update.message.reply_text(f"❌ Ошибка базы данных: {err}")

async def prompt_delete_item_simple(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Чтобы удалить тип товара, введите:\n"
        "/delete_item_type <название товара>"
    )



async def delete_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_company_admin(user_id):
        await update.message.reply_text("❌ У вас нет прав для этой команды.")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("⚠️ Используйте команду:\n/delete_company <ID компании>")
        return
    company_id = int(context.args[0])

    try:
        with db_connect() as (conn, cursor):
            cursor.execute("SET FOREIGN_KEY_CHECKS = 0;")

            for suffix in [
                "checklist_completions",
                "checklists",
                "tasks",
                "attendance",
                "bonuses",
                "collections",
                "collection_items"
            ]:
                tbl = f"company_{company_id}_{suffix}"
                cursor.execute(f"DROP TABLE IF EXISTS `{tbl}`;")

            cursor.execute("DELETE FROM subscriptions WHERE company_id = %s;", (company_id,))
            cursor.execute("DELETE FROM companies WHERE id = %s;", (company_id,))

            cursor.execute("SET FOREIGN_KEY_CHECKS = 1;")

        await update.message.reply_text(f"✅ Все данные о компании {company_id} удалены.")
    except mysql.connector.Error as err:
        await update.message.reply_text(f"❌ Ошибка при удалении: {err}")






async def purge_employee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id

    company_id = get_company_id(admin_id)
    if not company_id or admin_id not in ADMIN_IDS:
        await update.message.reply_text("❌ У вас нет прав на удаление сотрудников.")
        return

    if len(context.args) < 1 or not context.args[0].isdigit():
        await update.message.reply_text("⚠️ Используйте:\n/purge_employee <ID сотрудника> [yes]")
        return
    emp_id = int(context.args[0])

    if len(context.args) < 2 or context.args[1].lower() != "yes":
        await update.message.reply_text(
            f"⚠️ Вы собираетесь безвозвратно удалить ВСЕ данные сотрудника {emp_id}.\n\n"
            f"Если вы уверены, повторите команду так:\n"
            f"/purge_employee {emp_id} yes"
        )
        return

    try:
        with db_connect() as (conn, cursor):
            emp_table = get_table("employees", company_id)
            cursor.execute(f"DELETE FROM `{emp_table}` WHERE id = %s", (emp_id,))
        await update.message.reply_text(
            f"✅ Все данные сотрудника {emp_id} успешно удалены из компании {company_id}."
        )
    except mysql.connector.Error as err:
        await update.message.reply_text(f"❌ Ошибка при удалении сотрудника: {err}")


async def feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Связаться со мной можно здесь:\n"
        "👉 [@art_ooi](https://t.me/art_ooi)",
        parse_mode="Markdown"
    )

async def stocks_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Соберите все ваши кабинеты в один удобный Excel файл:\n"
        "👉 [@stocks_wildberries_bot](https://t.me/stocks_wildberries_bot)",
        parse_mode="Markdown"
    )

async def start_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db_connect() as (conn, cursor):
        # Старт
        cursor.execute("SELECT COUNT(*) FROM start_clicks")
        total_start = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM start_clicks WHERE clicked_at >= CURDATE()")
        today_start = cursor.fetchone()[0]
        # Регистрация компании
        cursor.execute("SELECT COUNT(*) FROM company_register_clicks")
        total_reg = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM company_register_clicks WHERE clicked_at >= CURDATE()")
        today_reg = cursor.fetchone()[0]
        # Войти
        cursor.execute("SELECT COUNT(*) FROM login_clicks")
        total_login = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM login_clicks WHERE clicked_at >= CURDATE()")
        today_login = cursor.fetchone()[0]
        # Что может бот
        cursor.execute("SELECT COUNT(*) FROM features_clicks")
        total_feat = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM features_clicks WHERE clicked_at >= CURDATE()")
        today_feat = cursor.fetchone()[0]

    await update.message.reply_text(
        f"🗓 За сегодня:\n"
        f"• Старт: {today_start}\n"
        f"• Регистрация компании: {today_reg}\n"
        f"• Войти: {today_login}\n"
        f"• Что может бот: {today_feat}\n\n"
        f"📊 Всего:\n"
        f"• Старт: {total_start}\n"
        f"• Регистрация компании: {total_reg}\n"
        f"• Войти: {total_login}\n"
        f"• Что может бот: {total_feat}"
    )



if __name__ == '__main__':
    try:
        app = ApplicationBuilder().token(os.getenv("BOT_TOKEN")).build()

        

        # ConversationHandler для добавления задачи
        conv_handler_add_task = ConversationHandler(
    entry_points=[CommandHandler("add_task", add_task),
        MessageHandler(filters.Regex(r"^➕ Добавить задачу$"), add_task)
    ],
    states={
        SET_TASK_DESCRIPTION: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, set_task_description)
        ],
        HANDLE_DEADLINE_BUTTON: [
            CallbackQueryHandler(handle_deadline_button, pattern=r"^deadline_")
        ],
        SET_TASK_DEADLINE: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_date_input)
        ],
        HANDLE_EMPLOYEE_BUTTON: [
            CallbackQueryHandler(handle_employee_button, pattern=r"^task_emp_")
        ],
    },
    fallbacks=[
        MessageHandler(filters.Regex(r"^⬅️ Назад$"), cancel_add_task)
    ],
    
)

        
        conv_handler_register_employee = ConversationHandler(
            entry_points=[MessageHandler(filters.TEXT & filters.Regex('^Регистрация сотрудника$'), register_employee_start)],
            states={
                EMPLOYEE_REGISTRATION_COMPANY: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_employee_company)]
            },
            fallbacks=[]
        )

        
        conv_handler_register_company = ConversationHandler(
            entry_points=[MessageHandler(filters.TEXT & filters.Regex(r'^Регистрация компании$'), register_company)],
            states={
                1: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_company)]
            },
            fallbacks=[]
        )

        # ConversationHandler для мОЙ СБОР
        conv_collections = ConversationHandler(
    entry_points=[MessageHandler(filters.Regex("^📦 Мой сбор$"), start_collection)],
    states={
        CHOOSE_TYPE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_collection_type)],
        ENTER_QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_quantity)],
        CONFIRM_MORE:   [MessageHandler(filters.Regex("^(Да|Нет)$"), confirm_more)],
    },
    fallbacks=[]
)
        conv_add_checklist_task = ConversationHandler(
            
    entry_points=[
        MessageHandler(filters.Regex(r'^➕ Добавить задачу в чек-лист$'), start_add_checklist_task)
    ],
    states={
        CHOOSE_EMPLOYEE_ID_FOR_CHECKLIST: [
            # только числа — все остальное уйдёт в fallback
            MessageHandler(filters.Regex(r'^\d+$'), choose_checklist_type)
        ],
        CHOOSE_TYPE_FOR_CHECKLIST: [
            # только daily, weekly или monthly
            MessageHandler(filters.Regex(TYPE_PATTERN), enter_checklist_description)
        ],
        ENTER_DESCRIPTION_FOR_CHECKLIST: [
            # любой текст (кроме команд и «⬅️ Назад»)
            MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex(r'^⬅️ Назад$'), save_checklist_task)
        ],
    },
    fallbacks=[
        # при любых других вводах («⬅️ Назад», «📬 Обратная связь» и т.п.)
        MessageHandler(filters.Regex(r'^⬅️ Назад$'), cancel_add_checklist_task)
    ]
)

        
        app.add_handler(CommandHandler("collections_report", collections_report))
        app.add_handler(CommandHandler("my_collections_today", my_collections_today))
        app.add_handler(CommandHandler("my_collections_avg", my_collections_avg))
        app.add_handler(CommandHandler("collections_avg_report", collections_avg_report))
        app.add_handler(CommandHandler("add_item_type", add_item_type))
        app.add_handler(MessageHandler(filters.Regex("^📦 Мой сбор за день$"), my_collections_today))
        app.add_handler(MessageHandler(filters.Regex("^📦 Мой сбор за месяц$"), my_collections_avg))
        # Основные обработчики для кнопок главного меню и команд
        app.add_handler(CommandHandler("start", start))
        app.add_handler(MessageHandler(filters.TEXT & filters.Regex('^🏠 Старт$'), start))
        app.add_handler(MessageHandler(filters.TEXT & filters.Regex('^Войти$'), login))
        app.add_handler(MessageHandler(filters.TEXT & filters.Regex('^👤 Мой профиль$'), my_profile))
        app.add_handler(MessageHandler(filters.TEXT & filters.Regex('^📑 Посмотреть задачи$'), view_tasks))
        app.add_handler(MessageHandler(filters.TEXT & filters.Regex('^😤 Выполнить задачу$'), complete_task))
        app.add_handler(MessageHandler(filters.TEXT & filters.Regex('^💸 Моя зарплата$'), my_profile))
        app.add_handler(MessageHandler(filters.TEXT & filters.Regex('^Я пришел$'), start_shift))
        app.add_handler(MessageHandler(filters.TEXT & filters.Regex('^Я ушел$'), end_shift))
        app.add_handler(MessageHandler(filters.TEXT & filters.Regex('^Мои посещения$'), my_visits))
        app.add_handler(MessageHandler(filters.TEXT & filters.Regex('^Мои зарплаты$'), my_salaries))
        app.add_handler(MessageHandler(filters.TEXT & filters.Regex('^👑 Админ$'), admin_commands))
        app.add_handler(MessageHandler(filters.TEXT & filters.Regex('^📋 Мои чеклисты$'), view_checklists))
        app.add_handler(MessageHandler(filters.Regex("^📅 Сегодняшний сбор$"), my_collections_today))
        app.add_handler(MessageHandler(filters.Regex("^📊 Среднее за месяц$"), my_collections_avg))
        app.add_handler(MessageHandler(filters.Regex("^📌 Управление сотрудниками$"), handle_admin_section))
        app.add_handler(MessageHandler(filters.Regex("^📝 Задачи$"), handle_admin_section))
        app.add_handler(MessageHandler(filters.Regex("^✅ Чек-листы$"), handle_admin_section))
        app.add_handler(MessageHandler(filters.Regex("^📊 Зарплаты и посещения$"), handle_admin_section))
        app.add_handler(MessageHandler(filters.Regex("^💰 Премии и штрафы$"), handle_admin_section))
        app.add_handler(MessageHandler(filters.Regex("^📦 Сборка$"), handle_admin_section))
        app.add_handler(MessageHandler(filters.Regex("^⚙️ Система$"), handle_admin_section))
        
    
       

        # 📌 Управление сотрудниками
        
        app.add_handler(MessageHandler(filters.Regex("^❌ Уволить сотрудника$"), delete_employee))
        app.add_handler(MessageHandler(filters.Regex("^👁 Список сотрудников$"), admin_view_employees))

# 📝 Задачи
        
        app.add_handler(MessageHandler(filters.Regex("^👁 Посмотреть задачи$"), view_tasks))
        app.add_handler(MessageHandler(filters.Regex("^✅ Отметить выполненной$"), complete_task))

# ✅ Чек-листы
        app.add_handler(MessageHandler(filters.Regex("^👁 Просмотр чек-листов$"), admin_view_employees_checklists))
        app.add_handler(MessageHandler(filters.Regex("^✅ Выполнить чек-лист$"), view_checklists))


# 📊 Зарплаты и посещения
        app.add_handler(MessageHandler(filters.Regex("^📈 Отчёт по зарплате$"), salary_report))
        app.add_handler(MessageHandler(filters.Regex("^👁 Посещения сотрудника$"), view_attendance))
        app.add_handler(MessageHandler(filters.Regex("^🛠 Оклад и норма$"), set_salary_and_hours))  # требует аргументов — можешь сделать отдельный handler с меню

# 💰 Премии и штрафы
        app.add_handler(MessageHandler(filters.Regex("^🎁 Премия$"), award_bonus))
        app.add_handler(MessageHandler(filters.Regex("^⚠️ Штраф$"), assign_penalty))

# 📦 Сборка
        app.add_handler(MessageHandler(filters.Regex("^📋 Отчёт по сборке$"), collections_report))
        app.add_handler(MessageHandler(filters.Regex("^📊 Средняя сборка$"), collections_avg_report))
        app.add_handler(MessageHandler(filters.Regex("^➕ Новый товар$"), add_item_type))

# ⚙️ Система
        app.add_handler(MessageHandler(filters.Regex("^🏢 Зарегистрировать компанию$"), register_company))
        app.add_handler(MessageHandler(filters.Regex("^🆔 ID компании$"), show_company_id))
        app.add_handler(MessageHandler(filters.Regex("^💳 Оплатить подписку$"), pay_subscription))

        app.add_handler(MessageHandler(filters.Regex("^🆔 Мой ID$"), show_my_id))

# Назад из подменю
        app.add_handler(MessageHandler(filters.Regex("^⬅️ Назад$"), handle_admin_back))
        app.add_handler(CommandHandler("award_bonus", award_bonus))
        app.add_handler(CommandHandler("assign_penalty", assign_penalty))
        
        app.add_handler(conv_handler_add_task)

        app.add_handler(CallbackQueryHandler(handle_complete_checklist_button, pattern="^complete_"))
        app.add_handler(CallbackQueryHandler(handle_callback)) #универсальный обработчик должнен быть внизу
        

        # Дополнительные командные обработчики
        app.add_handler(CommandHandler("view_employees_checklists", admin_view_employees_checklists))
        app.add_handler(CommandHandler("view_employee_checklists", admin_view_employee_checklists))
        app.add_handler(CommandHandler("complete_checklist", complete_checklist))
        app.add_handler(CommandHandler("my_id", show_my_id))
        app.add_handler(CommandHandler("set_salary_and_hours", set_salary_and_hours))
        app.add_handler(CommandHandler("my_full_salary", my_full_salary))
        app.add_handler(CommandHandler("my_profile", my_profile))
        app.add_handler(CommandHandler("show_company_id", show_company_id))
        app.add_handler(CommandHandler("view_tasks", view_tasks))
        app.add_handler(CommandHandler("complete_task", complete_task))
        app.add_handler(CommandHandler("salary", my_full_salary))
        app.add_handler(CommandHandler("salary_report", salary_report))
        app.add_handler(CommandHandler("view_attendance", view_attendance))
        app.add_handler(CommandHandler("delete_employee", delete_employee))
        app.add_handler(CommandHandler("pay_subscription", pay_subscription))
        
        app.add_handler(CommandHandler("checklist_report", checklist_report))
        app.add_handler(CommandHandler("show_reg", show_reg))
        app.add_handler(CommandHandler("start_stats", start_stats))
        app.add_handler(MessageHandler(filters.Regex("^📊 Отчет по чек-листам$"), checklist_report))
        
        app.add_handler(MessageHandler(filters.Regex('^💳 Моя подписка$'), my_subscription))
        app.add_handler(MessageHandler(filters.Regex(r"^❓ Что может этот бот$"), show_features))
    
        app.add_handler(CommandHandler("feedback", feedback))
        app.add_handler(CommandHandler("Stocks_bot", stocks_bot))
       

        app.add_handler(CommandHandler("delete_item_type", delete_item_type))
        app.add_handler(
    MessageHandler(filters.Regex(r"^➖ Удалить товар$"), prompt_delete_item_simple)
)
        

        # Добавляем ConversationHandler'ы
        app.add_handler(conv_handler_register_employee)
        app.add_handler(conv_handler_register_company)
        
        app.add_handler(conv_add_checklist_task)
        
        app.add_handler(conv_collections)

        app.add_handler(MessageHandler(
    filters.TEXT & ~filters.COMMAND & filters.Regex(r"^\d+$"),
    confirm_task_completion
))
        app.add_handler(MessageHandler(filters.Regex("^📬 Обратная связь$"), lambda update, context: update.message.reply_text(
    "📬 Если у вас есть вопросы, предложения или вы нашли баг — напишите разработчику:\n\n"
    "👉 [@art_ooi](https://t.me/art_ooi)", parse_mode="Markdown"
)))
        app.add_handler(CommandHandler("delete_company", delete_company))
        app.add_handler(CommandHandler("purge_employee", purge_employee))

        job_queue = app.job_queue
        job_queue.run_repeating(check_long_shifts, interval=3600, first=3600)

        print("Бот запущен...")
        app.run_polling()
    finally:
        pass
