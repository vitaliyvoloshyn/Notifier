from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models


class TelegramUserManager(BaseUserManager):
    def create_user(self, telegram_id, username=None, **extra_fields):
        if not telegram_id:
            raise ValueError("Telegram ID є обов'язковим")

        user = self.model(telegram_id=telegram_id, username=username, **extra_fields)
        # Оскільки пароля немає, встановлюємо невалідний пароль (користувач не зможе зайти через звичайну форму)
        user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, telegram_id, username=None, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)

        user = self.create_user(telegram_id=telegram_id, username=username, **extra_fields)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user


class TelegramUser(AbstractBaseUser, PermissionsMixin):
    # Telegram ID є унікальним для кожного користувача і ніколи не змінюється
    telegram_id = models.BigIntegerField(unique=True, primary_key=True)

    # Telegram username (може бути пустим, якщо користувач його не налаштував)
    username = models.CharField(max_length=32, blank=True, null=True)

    # Справжнє ім'я з Телеграму для привітань (First Name + Last Name)
    full_name = models.CharField(max_length=150, blank=True)

    # Додаткові поля для логіки додатка
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)  # Потрібно для доступу в адмінку (якщо знадобиться)
    date_joined = models.DateTimeField(auto_now_add=True)

    objects = TelegramUserManager()

    # Вказуємо Django, що замість username ми використовуємо telegram_id
    USERNAME_FIELD = 'telegram_id'
    REQUIRED_FIELDS = []  #  система завжди автоматично запитує поле, вказане в USERNAME_FIELD (у вашому випадку це telegram_id), а також password

    def __str__(self):
        return f"@{self.username}" if self.username else f"User {self.telegram_id}"
