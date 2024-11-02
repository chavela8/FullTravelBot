import unittest
from unittest.mock import Mock, patch
from telegram import Update
from FullTravelBot import start, help_command, echo

class TestFullTravelBot(unittest.TestCase):

    def setUp(self):
        self.update = Mock(spec=Update)
        self.context = Mock()

    def test_start(self):
        self.update.message.reply_text = Mock()
        start(self.update, self.context)
        self.update.message.reply_text.assert_called_with('Привет! Я FullTravelBot. Чем могу помочь?')

    def test_help(self):
        self.update.message.reply_text = Mock()
        help_command(self.update, self.context)
        self.update.message.reply_text.assert_called_with('Я могу помочь вам с планированием путешествия. Используйте /start для начала.')

    def test_echo(self):
        self.update.message.text = "Тестовое сообщение"
        self.update.message.reply_text = Mock()
        echo(self.update, self.context)
        self.update.message.reply_text.assert_called_with("Вы сказали: Тестовое сообщение")

if __name__ == '__main__':
    unittest.main()