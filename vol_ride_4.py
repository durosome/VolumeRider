import obspython as obs
import time
import math
from ctypes import (
    CDLL,
    Structure,
    POINTER,
    CFUNCTYPE,
    c_void_p,
    c_float,
    c_int,
    c_bool,
    c_char_p,
)

# ============================================================================
#  Минимальные ctypes‑обёртки для obs_volmeter и obs_source_release
# ============================================================================
try:
    _obsffi = CDLL("obs")
except Exception:
    _obsffi = None


class _Source(Structure):
    pass


class _Volmeter(Structure):
    pass


if _obsffi is not None:
    volmeter_callback_t = CFUNCTYPE(
        None, c_void_p, POINTER(c_float), POINTER(c_float), POINTER(c_float)
    )

    def _wrap(funcname, restype, argtypes):
        func = getattr(_obsffi, funcname)
        func.restype = restype
        func.argtypes = argtypes
        return func

    _c_obs_get_source_by_name = _wrap("obs_get_source_by_name", POINTER(_Source), [c_char_p])
    _c_obs_source_release = _wrap("obs_source_release", None, [POINTER(_Source)])
    _c_obs_volmeter_create = _wrap("obs_volmeter_create", POINTER(_Volmeter), [c_int])
    _c_obs_volmeter_destroy = _wrap("obs_volmeter_destroy", None, [POINTER(_Volmeter)])
    _c_obs_volmeter_add_callback = _wrap(
        "obs_volmeter_add_callback",
        None,
        [POINTER(_Volmeter), volmeter_callback_t, c_void_p],
    )
    _c_obs_volmeter_remove_callback = _wrap(
        "obs_volmeter_remove_callback",
        None,
        [POINTER(_Volmeter), volmeter_callback_t, c_void_p],
    )
    _c_obs_volmeter_attach_source = _wrap(
        "obs_volmeter_attach_source", c_bool, [POINTER(_Volmeter), POINTER(_Source)]
    )
    _c_obs_volmeter_detach_source = _wrap(
        "obs_volmeter_detach_source", None, [POINTER(_Volmeter)])
    _c_obs_volmeter_get_nr_channels = _wrap(
        "obs_volmeter_get_nr_channels", c_int, [POINTER(_Volmeter)])
else:
    volmeter_callback_t = None


# ============================================================================
#  Основной класс VolRider
# ============================================================================
class VolRider:
    def __init__(self):
        self.settings = None                 # последние настройки
        self.listen_volmeter = None          # постоянный вольтметр для listen
        self.last_magnitude = None           # последнее измеренное значение (dB)
        self.channels = 1                     # количество каналов listen
        self.smoothed_lufs = None             # сглаженный уровень (LUFS)
        self.current_gain_lin = 1.0            # текущий линейный коэффициент
        self.frozen = False                    # флаг заморозки (hold или ниже порога)
        self.bypass_active = False              # активен ли байпас
        self.debug = False                      # режим отладки (логгирование)

        # Период таймера 500 мс (как в оригинале)
        self.timer_period = 500

        # Счётчик для медленного режима (каждый 4-й тик)
        self.adjust_counter = 0

        # Для сохранения громкости при байпасе
        self.saved_volume = None
        self._current_listen = None            # для отслеживания изменений источника

    # ------------------------------------------------------------------------
    #  Работа с вольтметром
    # ------------------------------------------------------------------------
    def _volmeter_callback(self, data, mag, peak, input_peak):
        """Колбэк вольтметра – обновляет last_magnitude (усреднённый по каналам)."""
        if not mag:
            return
        total = 0.0
        for i in range(self.channels):
            total += float(mag[i])
        avg = total / self.channels
        self.last_magnitude = max(avg, -100.0)

    def _create_volmeter(self, source_name):
        """Создаёт и прикрепляет вольтметр к источнику source_name."""
        if _obsffi is None or not source_name:
            return False
        self._destroy_volmeter()

        src_c = _c_obs_get_source_by_name(source_name.encode("utf-8"))
        if not src_c:
            obs.script_log(obs.LOG_WARNING, f"Источник '{source_name}' не найден")
            return False

        vol = _c_obs_volmeter_create(0)
        if not vol:
            obs.script_log(obs.LOG_WARNING, "Не удалось создать вольтметр")
            _c_obs_source_release(src_c)
            return False

        self.volmeter_callback_func = volmeter_callback_t(self._volmeter_callback)
        _c_obs_volmeter_add_callback(vol, self.volmeter_callback_func, None)

        attached = _c_obs_volmeter_attach_source(vol, src_c)
        if not attached:
            obs.script_log(obs.LOG_WARNING, f"Не удалось прикрепить вольтметр к '{source_name}'")
            _c_obs_volmeter_remove_callback(vol, self.volmeter_callback_func, None)
            _c_obs_volmeter_destroy(vol)
            _c_obs_source_release(src_c)
            return False

        self.listen_volmeter = vol
        self.channels = max(1, _c_obs_volmeter_get_nr_channels(vol))
        _c_obs_source_release(src_c)
        obs.script_log(obs.LOG_INFO, f"Вольтметр для '{source_name}' создан, каналов: {self.channels}")
        return True

    def _destroy_volmeter(self):
        if self.listen_volmeter:
            _c_obs_volmeter_detach_source(self.listen_volmeter)
            _c_obs_volmeter_remove_callback(self.listen_volmeter, self.volmeter_callback_func, None)
            _c_obs_volmeter_destroy(self.listen_volmeter)
            self.listen_volmeter = None
            self.last_magnitude = None

    # ------------------------------------------------------------------------
    #  Сглаживание уровня (экспоненциальное скользящее среднее)
    # ------------------------------------------------------------------------
    def _update_smoothed_lufs(self, new_mag, frozen, alpha, hold, threshold):
        """
        Обновляет сглаженный уровень.
        Если frozen=True, значение не обновляется (заморозка).
        При выходе из заморозки сбрасываем сглаженное к текущему.
        """
        if frozen and not self.frozen:
            # Вход в заморозку
            self.frozen = True
            if self.debug:
                obs.script_log(obs.LOG_INFO,
                               f"Вход в заморозку: voltmeter={new_mag:.1f} dB, "
                               f"threshold={threshold} dB, hold={hold}")
            return

        if not frozen and self.frozen:
            # Выход из заморозки
            self.smoothed_lufs = new_mag
            self.frozen = False
            if self.debug:
                obs.script_log(obs.LOG_INFO,
                               f"Выход из заморозки: voltmeter={new_mag:.1f} dB, "
                               f"threshold={threshold} dB, hold={hold}, сброс smoothed_lufs")
            return

        if frozen:
            # Уже в заморозке, ничего не делаем
            return

        # Обычное обновление EMA
        if self.smoothed_lufs is None:
            self.smoothed_lufs = new_mag
        else:
            self.smoothed_lufs = alpha * new_mag + (1 - alpha) * self.smoothed_lufs

    # ------------------------------------------------------------------------
    #  Логика регулировки (вызывается по таймеру)
    # ------------------------------------------------------------------------
    def _adjust(self):
        if not self.settings or self.bypass_active:
            return

        listen_name = obs.obs_data_get_string(self.settings, "audio_listen")
        ctrl_name = obs.obs_data_get_string(self.settings, "audio_ctrl")
        if not listen_name or not ctrl_name:
            return

        # Режим атаки: Fast / Slow
        attack_mode = obs.obs_data_get_string(self.settings, "attack")
        if attack_mode == "Slow":
            # Медленный режим: регулируем каждый 4-й тик, период усреднения 10 секунд
            self.adjust_counter += 1
            if self.adjust_counter % 4 != 0:
                return
            alpha_ema = 0.0488  # 1 - exp(-0.5/10) ≈ 0.0488
        else:
            # Быстрый режим: регулируем каждый тик, период усреднения 5 секунд
            alpha_ema = 0.095   # 1 - exp(-0.5/5) ≈ 0.095

        # Проверяем наличие вольтметра
        if not self.listen_volmeter:
            self._create_volmeter(listen_name)
            return
        if self.last_magnitude is None:
            return

        target_lufs = obs.obs_data_get_double(self.settings, "target_lufs")
        threshold = obs.obs_data_get_int(self.settings, "threshold")
        hold = obs.obs_data_get_bool(self.settings, "hold")

        # Заморозка основывается на мгновенном значении last_magnitude
        frozen = hold or (self.last_magnitude < threshold)

        # Обновляем сглаженный уровень с актуальным alpha
        self._update_smoothed_lufs(self.last_magnitude, frozen, alpha_ema, hold, threshold)

        if self.smoothed_lufs is None:
            return

        # Вычисляем дельту и целевой коэффициент
        delta_db = target_lufs - self.smoothed_lufs
        delta_db = max(-30.0, min(20.0, delta_db))
        target_lin = 10 ** (delta_db / 20.0)
        target_lin = max(0.0, min(10.0, target_lin))

        # Плавное изменение текущего усиления
        self.current_gain_lin += (target_lin - self.current_gain_lin) * 0.5

        # Применяем громкость к control источнику
        src = obs.obs_get_source_by_name(ctrl_name)
        if src:
            try:
                obs.obs_source_set_volume(src, self.current_gain_lin)
                if self.debug:
                    obs.script_log(obs.LOG_INFO,
                                   f"listen: {self.smoothed_lufs:.1f} LUFS | "
                                   f"target: {target_lufs:.1f} | "
                                   f"delta: {delta_db:+.1f} dB | "
                                   f"gain: {self.current_gain_lin:.3f}")
            except Exception as e:
                obs.script_log(obs.LOG_ERROR, f"Ошибка установки громкости: {e}")
            finally:
                obs.obs_source_release(src)
        else:
            obs.script_log(obs.LOG_WARNING, f"Источник управления '{ctrl_name}' не найден")

    # ------------------------------------------------------------------------
    #  Публичные методы для вызова из OBS
    # ------------------------------------------------------------------------
    def update(self, settings):
        self.settings = settings

        listen = obs.obs_data_get_string(settings, "audio_listen")
        ctrl = obs.obs_data_get_string(settings, "audio_ctrl")
        if listen and ctrl and listen == ctrl:
            obs.script_log(obs.LOG_WARNING, "Источники прослушивания и управления совпадают! Это может вызвать обратную связь.")

        # Обработка байпаса
        bypass = obs.obs_data_get_bool(settings, "bypass")
        if bypass and not self.bypass_active:
            self.bypass_active = True
            obs.timer_remove(self._adjust)
            if ctrl:
                src = obs.obs_get_source_by_name(ctrl)
                if src:
                    self.saved_volume = obs.obs_source_get_volume(src)
                    obs.obs_source_set_volume(src, 1.0)
                    obs.obs_source_release(src)
                    obs.script_log(obs.LOG_INFO, f"Байпас включён – громкость '{ctrl}' установлена в 1.0")
        elif not bypass and self.bypass_active:
            self.bypass_active = False
            if self.saved_volume is not None and ctrl:
                src = obs.obs_get_source_by_name(ctrl)
                if src:
                    obs.obs_source_set_volume(src, self.saved_volume)
                    obs.obs_source_release(src)
                    obs.script_log(obs.LOG_INFO, f"Байпас выключен – восстановлена громкость {self.saved_volume:.3f}")
            obs.timer_add(self._adjust, self.timer_period)

        # Если источник listen изменился – пересоздаём вольтметр
        if listen != self._current_listen:
            if listen:
                self._create_volmeter(listen)
            else:
                self._destroy_volmeter()
            self._current_listen = listen
            self.smoothed_lufs = None

        self.debug = obs.obs_data_get_bool(settings, "debug")

    def load(self, settings):
        self.settings = settings
        self._current_listen = obs.obs_data_get_string(settings, "audio_listen")
        if self._current_listen:
            self._create_volmeter(self._current_listen)

        bypass = obs.obs_data_get_bool(settings, "bypass")
        if not bypass:
            obs.timer_add(self._adjust, self.timer_period)
        else:
            self.bypass_active = True

    def unload(self):
        obs.timer_remove(self._adjust)
        self._destroy_volmeter()

    def get_info_text(self):
        listen_name = obs.obs_data_get_string(self.settings, "audio_listen") if self.settings else ""
        ctrl_name = obs.obs_data_get_string(self.settings, "audio_ctrl") if self.settings else ""
        listen_level = f"{self.last_magnitude:.1f} dBFS" if self.last_magnitude is not None else "—"
        listen_lufs = f"{self.smoothed_lufs:.1f} LUFS" if self.smoothed_lufs is not None else "—"
        ctrl_gain = f"{self.current_gain_lin:.3f}"
        frozen = " (заморожено)" if self.frozen else ""
        return (f"Listen: {listen_name} | уровень: {listen_level} | сглаж.: {listen_lufs}{frozen}\n"
                f"Control: {ctrl_name} | громкость: {ctrl_gain}")


# ============================================================================
#  Глобальный экземпляр VolRider
# ============================================================================
vr = None


def script_description():
    return "VolRider (улучшенная версия с настраиваемым периодом LUFS)"


def script_properties():
    props = obs.obs_properties_create()

    obs.obs_properties_add_float_slider(props, "target_lufs", "Целевой уровень (LUFS)", -60.0, 0.0, 1.0)
    obs.obs_properties_add_int_slider(props, "threshold", "Порог заморозки (dB)", -80, 0, 1)

    attack_list = obs.obs_properties_add_list(props, "attack", "Режим атаки",
                                               obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
    obs.obs_property_list_add_string(attack_list, "Быстро (5 сек)", "Fast")
    obs.obs_property_list_add_string(attack_list, "Медленно (10 сек)", "Slow")

    obs.obs_properties_add_bool(props, "hold", "Hold (заморозка)")
    obs.obs_properties_add_bool(props, "bypass", "Байпас (отключить регулировку)")
    obs.obs_properties_add_bool(props, "debug", "Режим отладки (логгирование)")

    listen_list = obs.obs_properties_add_list(props, "audio_listen", "Источник для прослушивания",
                                              obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
    ctrl_list = obs.obs_properties_add_list(props, "audio_ctrl", "Источник для управления",
                                            obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)

    sources = obs.obs_enum_sources()
    for source in sources:
        name = obs.obs_source_get_name(source)
        obs.obs_property_list_add_string(listen_list, name, name)
        obs.obs_property_list_add_string(ctrl_list, name, name)
    obs.source_list_release(sources)

    info_prop = obs.obs_properties_add_text(props, "info", "Текущее состояние",
                                            obs.OBS_TEXT_INFO)
    if vr and vr.settings:
        info = vr.get_info_text()
        obs.obs_property_set_description(info_prop, info)

    return props


def script_defaults(settings):
    obs.obs_data_set_default_double(settings, "target_lufs", -18.0)
    obs.obs_data_set_default_int(settings, "threshold", -80)
    obs.obs_data_set_default_string(settings, "attack", "Fast")
    obs.obs_data_set_default_bool(settings, "hold", False)
    obs.obs_data_set_default_bool(settings, "bypass", False)
    obs.obs_data_set_default_bool(settings, "debug", False)


def script_update(settings):
    global vr
    if vr is None:
        vr = VolRider()
    vr.update(settings)


def script_load(settings):
    global vr
    if vr is None:
        vr = VolRider()
    vr.load(settings)


def script_unload():
    global vr
    if vr:
        vr.unload()
        vr = None