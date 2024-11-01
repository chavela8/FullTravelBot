import os
import logging
import time
from typing import Optional, Dict, Any, Tuple, List
from datetime import datetime, timedelta
from threading import Thread
from dataclasses import dataclass
from enum import Enum
import json
import redis
import stripe
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Float, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from prometheus_client import Counter, Histogram, start_http_server
from telegram import Update, ReplyKeyboardMarkup, ParseMode
from telegram.ext import (
    Updater, CommandHandler, MessageHandler, 
    Filters, CallbackContext, ConversationHandler,
    PicklePersistence
)
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import paypalrestsdk
from flask import Flask, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Инициализация логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Инициализация базы данных
Base = declarative_base()

class PaymentProvider(Enum):
    PAYPAL = "paypal"
    STRIPE = "stripe"
    CRYPTO = "crypto"

class Language(Enum):
    EN = "en"  # Английский
    RU = "ru"  # Русский
    ES = "es"  # Испанский
    ZH = "zh"  # Китайский

class User(Base):
    __tablename__ = 'users'
    
    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True)
    language = Column(String(2))
    created_at = Column(DateTime, default=datetime.utcnow)
    last_active = Column(DateTime)
    preferences = Column(JSON)

class Transaction(Base):
    __tablename__ = 'transactions'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer)
    amount = Column(Float)
    currency = Column(String(3))
    provider = Column(String(50))
    status = Column(String(20))
    created_at = Column(DateTime, default=datetime.utcnow)
    payment_data = Column(JSON)

@dataclass
class Config:
    """Расширенный класс конфигурации"""
    BOT_TOKEN: str = os.getenv('BOT_TOKEN')
    GOOGLE_SHEET_ID: str = os.getenv('GOOGLE_SHEET_ID')
    PAYPAL_CLIENT_ID: str = os.getenv('PAYPAL_CLIENT_ID')
    PAYPAL_CLIENT_SECRET: str = os.getenv('PAYPAL_CLIENT_SECRET')
    STRIPE_SECRET_KEY: str = os.getenv('STRIPE_SECRET_KEY')
    DATABASE_URL: str = os.getenv('DATABASE_URL')
    REDIS_URL: str = os.getenv('REDIS_URL')
    RATE_LIMIT: int = int(os.getenv('RATE_LIMIT', '60'))
    SUPPORTED_LANGUAGES: List[str] = ['en', 'ru', 'es', 'zh']

    @classmethod
    def validate(cls) -> bool:
        required_vars = [
            'BOT_TOKEN', 'DATABASE_URL', 'REDIS_URL',
            'PAYPAL_CLIENT_ID', 'STRIPE_SECRET_KEY'
        ]
        return all(os.getenv(var) for var in required_vars)

class Analytics:
    """Менеджер аналитики с использованием метрик Prometheus"""
    def __init__(self):
        self.command_counter = Counter(
            'bot_commands_total',
            'Общее количество полученных команд',
            ['command']
        )
        self.payment_counter = Counter(
            'payments_total',
            'Общее количество обработанных платежей',
            ['provider', 'status']
        )
        self.response_time = Histogram(
            'response_time_seconds',
            'Время ответа в секундах',
            ['endpoint']
        )

class DatabaseManager:
    """Класс управления базой данных"""
    def __init__(self, database_url: str):
        self.engine = create_engine(database_url)
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)

    def get_session(self) -> Session:
        return self.SessionLocal()

    async def get_user(self, telegram_id: int) -> Optional[User]:
        with self.get_session() as session:
            return session.query(User).filter_by(telegram_id=telegram_id).first()

    async def create_user(self, telegram_id: int, language: str) -> User:
        with self.get_session() as session:
            user = User(
                telegram_id=telegram_id,
                language=language,
                created_at=datetime.utcnow(),
                last_active=datetime.utcnow()
            )
            session.add(user)
            session.commit()
            return user

class RateLimiter:
    """Ограничение частоты запросов с использованием Redis"""
    def __init__(self, redis_url: str, limit: int = 60):
        self.redis = redis.from_url(redis_url)
        self.limit = limit

    async def check_rate_limit(self, user_id: int) -> bool:
        current = int(time.time())
        key = f"rate_limit:{user_id}"
        
        pipe = self.redis.pipeline()
        pipe.zadd(key, {current: current})
        pipe.zremrangebyscore(key, 0, current - 60)
        pipe.zcard(key)
        pipe.expire(key, 60)
        results = pipe.execute()
        
        return results[2] <= self.limit

class Translations:
    """Управление многоязычной поддержкой"""
    def __init__(self):
        self.translations = {
            'en': {
                'welcome': "Welcome to Travel Assistant!",
                'select_service': "Please select a service:",
                'payment_error': "Payment error occurred.",
                'feedback_thanks': "Thank you for your feedback!"
            },
            'ru': {
                'welcome': "Добро пожаловать в помощник путешественника!",
                'select_service': "Пожалуйста, выберите услугу:",
                'payment_error': "Произошла ошибка оплаты.",
                'feedback_thanks': "Спасибо за ваш отзыв!"
            },
            'es': {
                'welcome': "¡Bienvenido al Asistente de Viajes!",
                'select_service': "Por favor, seleccione un servicio:",
                'payment_error': "Ocurrió un error en el pago.",
                'feedback_thanks': "¡Gracias por su comentario!"
            },
            'zh': {
                'welcome': "欢迎使用旅行助手！",
                'select_service': "请选择服务：",
                'payment_error': "支付发生错误。",
                'feedback_thanks': "感谢您的反馈！"
            }
        }

    def get_text(self, key: str, language: str) -> str:
        return self.translations.get(language, self.translations['en']).get(
            key, self.translations['en'][key]
        )

class PaymentManager:
    """Управление несколькими платежными системами"""
    def __init__(self, config: Config):
        self.config = config
        self._initialize_providers()

    def _initialize_providers(self):
        # Инициализация PayPal
        paypalrestsdk.configure({
            "mode": "sandbox",
            "client_id": self.config.PAYPAL_CLIENT_ID,
            "client_secret": self.config.PAYPAL_CLIENT_SECRET
        })
        
        # Инициализация Stripe
        stripe.api_key = self.config.STRIPE_SECRET_KEY

    async def create_payment(
        self,
        amount: float,
        currency: str,
        provider: PaymentProvider,
        description: str
    ) -> Optional[Dict[str, Any]]:
        try:
            if provider == PaymentProvider.PAYPAL:
                return await self._create_paypal_payment(amount, currency, description)
            elif provider == PaymentProvider.STRIPE:
                return await self._create_stripe_payment(amount, currency, description)
            else:
                raise ValueError(f"Неподдерживаемый провайдер платежей: {provider}")
        except Exception as e:
            logger.error(f"Ошибка создания платежа: {e}")
            return None

    async def _create_paypal_payment(
        self,
        amount: float,
        currency: str,
        description: str
    ) -> Optional[Dict[str, Any]]:
        payment = paypalrestsdk.Payment({
            "intent": "sale",
            "payer": {"payment_method": "paypal"},
            "transactions": [{
                "amount": {
                    "total": f"{amount:.2f}",
                    "currency": currency
                },
                "description": description
            }],
            "redirect_urls": {
                "return_url": "https://your-domain.com/payment/success",
                "cancel_url": "https://your-domain.com/payment/cancel"
            }
        })

        if payment.create():
            return {
                'id': payment.id,
                'links': payment.links,
                'provider': PaymentProvider.PAYPAL.value
            }
        return None

    async def _create_stripe_payment(
        self,
        amount: float,
        currency: str,
        description: str
    ) -> Optional[Dict[str, Any]]:
        try:
            session = stripe.checkout.Session.create(
                payment_method_types=['card'],
                line_items=[{
                    'price_data': {
                        'currency': currency,
                        'unit_amount': int(amount * 100),
                        'product_data': {
                            'name': description,
                        },
                    },
                    'quantity': 1,
                }],
                mode='payment',
                success_url='https://your-domain.com/success',
                cancel_url='https://your-domain.com/cancel',
            )
            return {
                'id': session.id,
                'url': session.url,
                'provider': PaymentProvider.STRIPE.value
            }
        except Exception as e:
            logger.error(f"Ошибка создания платежа Stripe: {e}")
            return None

class EnhancedTravelBot:
    """Расширенный основной класс бота с сохранением состояния и аналитикой"""
    def __init__(self, config: Config):
        self.config = config
        self.db = DatabaseManager(config.DATABASE_URL)
        self.analytics = Analytics()
        self.rate_limiter = RateLimiter(config.REDIS_URL, config.RATE_LIMIT)
        self.translations = Translations()
        self.payment_manager = PaymentManager(config)

    async def start(self, update: Update, context: CallbackContext) -> int:
        """Расширенная команда start с аналитикой и ограничением частоты запросов"""
        try:
            user_id = update.effective_user.id
            
            # Проверка ограничения частоты запросов
            if not await self.rate_limiter.check_rate_limit(user_id):
                await update.message.reply_text(
                    "Слишком много запросов. Пожалуйста, попробуйте позже."
                )
                return ConversationHandler.END

            # Запись аналитики
            self.analytics.command_counter.labels(command='start').inc()

            # Получение или создание пользователя
            user = await self.db.get_user(user_id)
            if not user:
                user = await self.db.create_user(
                    user_id,
                    update.effective_user.language_code or 'en'
                )

            # Отправка приветственного сообщения на языке пользователя
            welcome_text = self.translations.get_text('welcome', user.language)
            await update.message.reply_text(
                welcome_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self._get_keyboard(user.language)
            )

            return SELECTING_SERVICE

        except Exception as e:
            logger.error(f"Ошибка в команде start: {e}")
            await self._handle_error(update, context)
            return ConversationHandler.END

    def _get_keyboard(self, language: str) -> ReplyKeyboardMarkup:
        """Получение клавиатуры на языке пользователя"""
        keyboard = [
            ["🛡 Страховка", "🌐 Переводы"],
            ["🍽 Рестораны", "✈️ Авиабилеты"],
            ["🏨 Отели", "🔎 Другое"]
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    @staticmethod
    async def _handle_error(update: Update, context: CallbackContext) -> None:
        """Расширенный обработчик ошибок с аналитикой"""
        error_msg = "Произошла ошибка. Пожалуйста, попробуйте позже."
        if update.effective_message:
            await update.effective_message.reply_text(error_msg)

def create_flask_app(
    config: Config,
    db_manager: DatabaseManager,
    analytics: Analytics
) -> Flask:
    """Создание Flask приложения с ограничением частоты запросов и аналитикой"""
    app = Flask(__name__)
    limiter = Limiter(
        app,
        key_func=get_remote_address,
        default_limits=["200 per day", "50 per hour"]
    )

    @app.route('/webhook/paypal', methods=['POST'])
    @limiter.limit("10 per minute")
    def paypal_webhook():
        try:
            webhook_data = request.json
            return jsonify({'status': 'success'}), 200
        except Exception as e:
            logger.error(f"Ошибка вебхука PayPal: {e}")
            return jsonify({'status': 'error'}), 500

    @app.route('/webhook/stripe', methods=['POST'])
    @limiter.limit("10 per minute")
    def stripe_webhook():
        try:
            webhook_data = request.json
            return jsonify({'status': 'success'}), 200
        except Exception as e:
            logger.error(f"Ошибка вебхука Stripe: {e}")
            return jsonify({'status': 'error'}), 500

    return app

def main():
    """Расширенная главная функция со всеми функциями"""
    try:
        # Проверка конфигурации
        if not Config.validate():
            logger.error("Неверная конфигурация")
            return

        config = Config()
        
        # Инициализация бота с сохранением  состояния
        persistence = PicklePersistence(filename='bot_persistence')
        bot = EnhancedTravelBot(config)
        updater = Updater(config.BOT_TOKEN, persistence=persistence)
        
        # Запуск сервера метрик Prometheus
        start_http_server(8000)
        
        # Настройка обработчика разговора
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler('start', bot.start)],
            states={
                SELECTING_SERVICE: [
                    MessageHandler(
                        Filters.text & ~Filters.command,
                        bot.handle_service_selection
                    )
                ],
                AWAITING_PAYMENT: [
                    MessageHandler(
                        Filters.text & ~Filters.command,
                        bot.handle_service_selection
                    )
                ]
            },
            fallbacks=[CommandHandler('start', bot.start)],
            persistent=True,
            name='main_conversation'
        )

        # Добавление обработчиков
        dispatcher = updater.dispatcher
        dispatcher.add_handler(conv_handler)
        
        # Создание и запуск Flask приложения
        flask_app = create_flask_app(config, bot.db, bot.analytics)
        Thread(target=lambda: flask_app.run(port=5000, debug=False)).start()
        
        # Запуск бота
        updater.start_polling()
        logger.info("Бот успешно запущен!")
        updater.idle()

    except Exception as e:
        logger.error(f"Критическая ошибка в main: {e}")

if __name__ == '__main__':
    main()