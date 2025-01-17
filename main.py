import re
import json
import asyncio
from urllib.parse import urljoin, unquote, quote

import aiohttp
from bs4 import BeautifulSoup
import requests

import logging

# Что-то типа такого было бы круто.
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

file_handler = logging.FileHandler('logs.log')
file_handler.setLevel(logging.INFO)

file_handler.setFormatter(formatter)
logging.getLogger('').addHandler(file_handler)


class Parser:
    '''
    Асинхронный парсер сайта https://ru.wikipedia.org/,
    определяющий путь переходов от стартовой до финальной страницы
    и предложения с нужными ссылками для осуществления этого перехода.
    При создании экземпляра класса необходимы данные
    о стартовой и финальной страницы (считывает из json).'''

    def __init__(self, start_url, final_url):
        self.MAIN_DOC_URL = 'https://ru.wikipedia.org/'
        self.start_url = unquote(start_url)
        self.final_url = unquote(final_url)
        self.search_queue = []
        self.ckecked_urls = set()
        self.next_queue = []
        self.way_to_final_url = ''

    @staticmethod
    def get_soup(url):
        '''
        Запрашивает страницу и парсит ее, возвращая "суп"
        (используется для отдельных разовых запросов).
        '''
        try:
            response = requests.get(url)
            response.encoding = 'utf-8'
            return BeautifulSoup(response.text, 'lxml')
        except Exception as e:
            ''' Обрабатывать так исключения неправильно,
            отлавливать нужно конкретные исключение, а не все сразу. В данном случае будет что-то типа
            requests.exceptions.RequestException | requests.exceptions.Timeout | requests.exceptions.InvalidURL
            '''
            logging.error(f'{e} || URL - {url}')

    def get_urls_list(self, start_url):
        '''
        Собирает со страницы и возвращает список ссылок нужного формата.
        Используется только для составления первоначального списка ссылок
        для дальнейшего запуска асинхронного поиска по ним.
        '''
        soup = self.get_soup(start_url)
        urls_list = list(set(
            unquote(url['href']) for url in soup.select('div.mw-body-content p a[href^="/wiki/"]')
        ))
        return urls_list

    def get_text(self, way_to_final_url):
        '''
        Получает на вход путь от стартовой до финальной ссылки в формате
        стартовая ссылка -> промежуточные ссылки -> финальная ссылка
        и извлекает из соответствующих страниц предложения, содержащие
        эти ссылки. Выводит результат в консоль согласно ТЗ.
        '''
        urls = way_to_final_url.split(' -> ')
        for i in range(len(urls) - 1):
            current_url = urls[i]
            next_url = urls[i + 1]
            if 'https' not in current_url:
                current_url = urljoin(self.MAIN_DOC_URL, current_url)
            soup = self.get_soup(current_url)
            pattern = " ".join(next_url.replace('/wiki/', '').split('_'))
            selector = f'div.mw-body-content p a[title="{pattern}"]'
            paragr_with_link = soup.select_one(selector).parent
            pattern = r'[A-ZА-Я0-9].+?href="' + re.escape(quote(next_url)) + '".+?\\.'
            sentence_with_tags = re.search(pattern, str(paragr_with_link))
            result_sentence = ' '.join(
                map(str.strip, re.split(
                    r'<.+?>', sentence_with_tags.group()))
            )
            print(f'{i + 1} ------------------------')
            print(
                result_sentence,
                urljoin(self.MAIN_DOC_URL, next_url),
                sep='\n', end='\n\n'
            )

    async def get_data(self, session, key, url):
        '''
        Асинхронно собирает ссылки с указанной страницы
        и формирует из них список для следующего цикла поиска
        (в страницах еще на уровень дальше от стартовой).
        Список составляется из кортежей ключ-ссылка,
        где ключом является путь от стартовой странице к текущей,
        а ссылкой - новая ссылка, которую будем проверять в следующем цикле.
        '''
        try:
            async with session.get(url=url) as response:
                logging.info(f"Посещена страница: {url}")
                resp = await response.text()
                soup = BeautifulSoup(resp, 'lxml')
                urls_list = list(set(
                    unquote(url['href']) for url in soup.select(
                        'div.mw-body-content p a[href^="/wiki/"]')))
                self.next_queue.append((
                    f'{key} -> {url.replace("https://ru.wikipedia.org", "")}',
                    urls_list
                ))
        except RuntimeError as e:
            logging.error(f"{e} || {url}")

    async def create_tasks(self):
        '''
        Берет в работу список ссылок для обхода
        (при первом запуске - ссылки стартовой страницы,
        далее - ссылки, собранные за предыдущую итерацию),
        проверяет, нет ли в нем искомой ссылки.
        Если нет, формирует и возвращает список задач для цикла событий.
        Также контролирует, чтобы уже пройденные ссылки
        не проходились повторно.
        '''
        tasks = []
        if self.next_queue:
            self.search_queue = self.next_queue
            self.next_queue = []
        async with aiohttp.ClientSession() as session:
            for key, urls_list in self.search_queue:
                for link in urls_list:
                    if link not in self.ckecked_urls:
                        url = urljoin(self.MAIN_DOC_URL, link)
                        if url == self.final_url:
                            self.way_to_final_url = f'{key} -> {link}'
                            return
                        task = asyncio.create_task(
                            self.get_data(session, key, url))
                        tasks.append(task)
                        self.ckecked_urls.add(link)
            await asyncio.gather(*tasks)

    def main(self):
        '''
        Формирует первоначальный список ссылок для проверки
        (ссылки стартовой страницы) и, если среди них нет искомой,
        запускает цикл событий
        (сначала - по ссылкам стартовой страницы,
        затем - по ссылкам страниц-детей первого уровня,
        затем - по ссылкам страниц-детей второго уровня
        и тд пока путь не будет найден).
        Запускает функцию получения предложений, содержащих нужные ссылки.
        Записывает в лог список проверенных ссылок.
        '''
        urls_list = self.get_urls_list(self.start_url)
        ''' 
        Нейминг переменных:
        Не стоит использовать префиксы и постфиксы типа set | list и прочее. Сам таким болею иногда,
        но в целом - плохая практика, в данном случае urls и self.get_urls было бы достаточно.
        '''
        if self.start_url in urls_list:
            self.way_to_final_url = self.start_url
        else:
            self.search_queue = [(self.start_url, urls_list)]
            asyncio.set_event_loop_policy(
                asyncio.DefaultEventLoopPolicy()
            )
            '''Вот тут не запустится на других операционных системах.
            Лучше использовать DefaultEventLoopPolicy. Насколько я понял из доки,
             он должен на винде работать, но протестить не могу :)
            '''
        while not self.way_to_final_url:
            asyncio.run(self.create_tasks())

        self.get_text(self.way_to_final_url)
        with open('logs.txt', 'w', encoding='utf-8') as file:
            ''' Как-будто бы правильно логировать в процессе то,
            что делаешь, а не после просто пусть кидать в файл
            '''
            for ind, url in enumerate(self.ckecked_urls, 1):
                file.write(f'{ind}. {url}\n')


if __name__ == '__main__':
    with open('data.json') as file:
        data = json.load(file)
        start_url = data.get('start_url')
        final_url = data.get('final_url')
    try:
        Parser(start_url, final_url).main()
    except Exception as e:
        logging.error(e)
        "А вот тут можно ловить все, чтобы у тебя в целом приложение не падало, а завершало работу корректно"

''' 
++ За алгоритм поиска.
+ за то, что это в целом сделано через async

Какие глобальные вещи тут увидел:
- Отсутствует типизация в целом. Сильно повышает читаемость кода указание типов.
- Круто выносить всякие зашитые константы, регулярки и прочее куда-то.
Если задача небольшая, как тут - можно просто в начале файла в апперкейсе объявить, но совсем правильно - 
в отдельный конфигурационный файл

З.Ы. Нет брейкпоинта по глубине поиска, как и в целом нет обработки случая, если страницы нет. 
В условии задачи про это не было написано, так, как идея.
По PEP 8 все соблюдено, но докстринги к методам объявляются в двойных ковычках, а не в одинарных.
Вообще если у тебя асинхронный код - то все функции должны быть асинхронными.
Хочется как можно больше ввода- вывода зашить в задачи, но у тебя в целом ок все сделано.
'''
