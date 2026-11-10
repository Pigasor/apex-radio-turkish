"""
Réponses de l'ingénieur aux questions du pilote (étape 3).

On repère des mots-clés dans la phrase reconnue et on répond avec les vraies
données de course. Plusieurs sujets dans la même phrase = plusieurs réponses.
"""

from __future__ import annotations

import re
import unicodedata

from strategy import Plan
from telemetry import Snapshot
from i18n import L, dec


def _norm(t: str) -> str:
    t = unicodedata.normalize("NFD", t.lower().replace("ı", "i"))   # le « ı » turc n'est pas décomposé par NFD
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9 ]", " ", t)


def _f(v: float, d: int = 1) -> str:
    return dec(f"{v:.{d}f}".replace(".0", "")) if d else f"{v:.0f}"


def _laptime(s: float | None) -> str:
    if not s:
        return L("pas encore de temps", "no time yet", "henüz zaman yok")
    m, r = divmod(s, 60)
    return dec(L(f"{int(m)} minute {r:.1f}", f"{int(m)} minute {r:.1f}", f"{int(m)} dakika {r:.1f}")) if m else dec(L(f"{r:.1f} secondes", f"{r:.1f} seconds", f"{r:.1f} saniye"))


def _tyre_names(idx: list[int]) -> str:
    names = L("avant gauche|avant droit|arrière gauche|arrière droit", "front left|front right|rear left|rear right", "ön sol|ön sağ|arka sol|arka sağ").split("|")
    return ", ".join(names[i] for i in idx)


# --------------------------------------------------------------------- sujets
def fuel(s: Snapshot, p: Plan) -> str:
    out = L(f"Il te reste {_f(s.fuel, 0)} litres.", f"You have {_f(s.fuel, 0)} litres left.", f"{_f(s.fuel, 0)} litre yakıtın kaldı.")
    if p.fuel_per_lap:
        out += L(f" Tu consommes {_f(p.fuel_per_lap)} par tour, ça fait {_f(s.fuel / p.fuel_per_lap)} tours d'essence.",
                 f" You're using {_f(p.fuel_per_lap)} a lap, that's {_f(s.fuel / p.fuel_per_lap)} laps of fuel.",
                 f" Tur başına {_f(p.fuel_per_lap)} harcıyorsun, bu {_f(s.fuel / p.fuel_per_lap)} turluk yakıt eder.")
    else:
        out += L(" J'ai besoin d'encore un ou deux tours propres pour calculer la conso.",
                 " I need one or two more clean laps to work out consumption.",
                 " Tüketimi hesaplamak için bir iki temiz tur daha lazım.")
    if p.stops_needed == 0:
        out += L(" C'est bon pour aller au bout.", " That's enough to the finish.", " Sona kadar yeter.")
    if p.save_per_lap:
        out += L(f" Si tu économises {_f(p.save_per_lap)} litre par tour, on évite l'arrêt.",
                 f" Save {_f(p.save_per_lap)} litres a lap and we skip the stop.",
                 f" Tur başına {_f(p.save_per_lap)} litre tasarruf edersen pit stopu atlarız.")
    return out


def energy(s: Snapshot, p: Plan) -> str:
    if s.energy is None:
        return L("Pas d'énergie virtuelle sur cette voiture, c'est l'essence qui compte.",
                 "No virtual energy on this car, fuel is what counts.",
                 "Bu araçta sanal enerji yok, önemli olan yakıt.")
    out = L(f"Énergie à {_f(s.energy * 100, 0)} %.", f"Energy at {_f(s.energy * 100, 0)} %.", f"Enerji %{_f(s.energy * 100, 0)}.")
    if p.energy_per_lap:
        out += L(f" {_f(p.energy_per_lap * 100)} % par tour, soit {_f(s.energy / p.energy_per_lap)} tours.",
                 f" {_f(p.energy_per_lap * 100)} % a lap, so {_f(s.energy / p.energy_per_lap)} laps.",
                 f" Tur başına %{_f(p.energy_per_lap * 100)}, yani {_f(s.energy / p.energy_per_lap)} tur.")
    return out


def laps(s: Snapshot, p: Plan) -> str:
    parts = []
    if s.max_laps:
        parts.append(L(f"Il reste {max(0, s.max_laps - s.laps_done)} tours.", f"{max(0, s.max_laps - s.laps_done)} laps to go.", f"{max(0, s.max_laps - s.laps_done)} tur kaldı."))
    elif s.time_left is not None:
        m = int(s.time_left // 60)
        parts.append(L(f"Il reste {m} minutes", f"{m} minutes to go", f"{m} dakika kaldı")
                     + (L(f", environ {_f(p.laps_to_finish, 0)} tours.", f", about {_f(p.laps_to_finish, 0)} laps.", f", yaklaşık {_f(p.laps_to_finish, 0)} tur.") if p.laps_to_finish else "."))
    if not parts:
        return L(f"Tu as bouclé {s.laps_done} tours.", f"You've done {s.laps_done} laps.", f"{s.laps_done} tur tamamladın.")
    return " ".join(parts)


def pit(s: Snapshot, p: Plan) -> str:
    if s.session != "course":
        return L("On n'est pas en course, pas de stratégie d'arrêt pour l'instant.", "We're not racing, no pit strategy for now.", "Yarışta değiliz, şimdilik pit stratejisi yok.")
    if not p.ready:
        return L("Je calcule encore, donne-moi un ou deux tours propres.", "Still calculating, give me one or two clean laps.", "Hâlâ hesaplıyorum, bana bir iki temiz tur ver.")
    if p.stops_needed == 0:
        return L("Pas besoin de rentrer, tu as de quoi finir.", "No need to pit, you can make the finish.", "Pite girmene gerek yok, sona kadar gidebilirsin.")
    if p.laps_until_box is not None and p.laps_until_box <= 1:
        when = L("Box ce tour.", "Box this lap.", "Bu tur box.")
    else:
        when = L(f"Tu rentres à la fin du tour {p.box_lap}, dans {p.laps_until_box} tours.",
                 f"You pit at the end of lap {p.box_lap}, in {p.laps_until_box} laps.",
                 f"{p.box_lap}. turun sonunda, yani {p.laps_until_box} tur sonra pite giriyorsun.")
    serv = []
    if p.fuel_to_add is not None:
        serv.append(L(f"{_f(p.fuel_to_add, 0)} litres", f"{_f(p.fuel_to_add, 0)} litres of fuel", f"{_f(p.fuel_to_add, 0)} litre yakıt"))
    if p.energy_target is not None:
        serv.append(L(f"énergie à {_f(p.energy_target * 100, 0)} %", f"energy to {_f(p.energy_target * 100, 0)} %", f"enerji %{_f(p.energy_target * 100, 0)}"))
    if p.tyre_compound == "pluie":
        serv.append(L("pneus pluie", "wet tyres", "yağmur lastiği"))
    elif p.tyre_compound == "slicks":
        serv.append(L("retour en slicks", "back to slicks", "slick lastiğe dönüş"))
    elif p.change_tyres:
        serv.append(L("4 pneus neufs", "four new tyres", "dört yeni lastik"))
    elif p.change_tyres is False:
        serv.append(L("on garde les pneus", "we keep the tyres", "lastikler kalıyor"))
    extra = L(f" Il faudra {p.stops_needed} arrêts au total.", f" {p.stops_needed} stops in total.", f" Toplam {p.stops_needed} pit stop.") if p.stops_needed and p.stops_needed > 1 else ""
    return (L(f"{when} On met {', '.join(serv)}.", f"{when} {', '.join(serv)}.", f"{when} {', '.join(serv)}.") + extra) if serv else when + extra


def tyres(s: Snapshot, p: Plan) -> str:
    wear = [w * 100 for w in s.tyre_wear]
    worst = min(range(4), key=lambda i: wear[i])
    out = L(f"Pneus à {_f(sum(wear) / 4, 0)} % en moyenne, le plus usé c'est l'{_tyre_names([worst])} à {_f(wear[worst], 0)} %.",
            f"Tyres at {_f(sum(wear) / 4, 0)} % on average, the most worn is the {_tyre_names([worst])} at {_f(wear[worst], 0)} %.",
            f"Lastikler ortalama %{_f(sum(wear) / 4, 0)}, en aşınmış olan {_tyre_names([worst])}, %{_f(wear[worst], 0)}.")
    if p.tyre_laps_left is not None:
        out += L(f" Encore environ {_f(p.tyre_laps_left, 0)} tours avant qu'ils soient vraiment usés.",
                 f" About {_f(p.tyre_laps_left, 0)} more laps before they're really worn.",
                 f" Gerçekten aşınmalarına yaklaşık {_f(p.tyre_laps_left, 0)} tur var.")
    temps = s.tyre_temp
    if any(temps):
        out += L(f" Températures autour de {_f(sum(temps) / 4, 0)} degrés.", f" Temperatures around {_f(sum(temps) / 4, 0)} degrees.", f" Sıcaklıklar {_f(sum(temps) / 4, 0)} derece civarında.")
    return out


def _sec(g: float) -> str:
    return L(f"{_f(g)} seconde{'s' if g >= 2 else ''}", f"{_f(g)} second{'s' if g != 1 else ''}", f"{_f(g)} saniye")


def gaps(s: Snapshot, p: Plan) -> str:
    # écart dans ta catégorie d'abord (celui de LMU est souvent vide), sinon l'écart général
    ga = s.gap_class_ahead if s.gap_class_ahead is not None else s.gap_ahead
    gb = s.gap_class_behind if s.gap_class_behind is not None else s.gap_behind
    t = LAST_TEXT
    front = bool(re.search(r"devant|ahead|in front|attaque|premier|leader|tete|ondeki|onumde|\blider", t))
    back = bool(re.search(r"derriere|behind|revient|catching|suit|arkadaki|arkamda|yaklasiyor|yaklasan", t))
    leader = s.ahead is None and (s.class_position or s.position) == 1
    if front and not back:
        if ga is not None:
            return L(f"La voiture devant est à {_sec(ga)}.", f"The car ahead is {_sec(ga)} up the road.", f"Öndeki araç {_sec(ga)} ileride.")
        if leader:
            return L("Tu es en tête, il n'y a personne devant toi.", "You're leading, there's nobody ahead.", "Lidersin, önünde kimse yok.")
        return L("Je n'arrive pas à lire l'écart avec la voiture devant pour l'instant.", "I can't read the gap to the car ahead right now.", "Öndeki araçla farkı şu an okuyamıyorum.")
    if back and not front:
        if gb is not None:
            return L(f"La voiture derrière est à {_sec(gb)}.", f"The car behind is {_sec(gb)} back.", f"Arkadaki araç {_sec(gb)} geride.")
        return L("Personne derrière toi pour l'instant.", "Nobody behind you right now.", "Şu an arkanda kimse yok.")
    parts = []
    if ga is not None:
        parts.append(L(f"{_sec(ga)} devant", f"{_sec(ga)} ahead", f"öndeki {_sec(ga)}"))
    elif leader:
        parts.append(L("tu es en tête", "you're leading", "lidersin"))
    if gb is not None:
        parts.append(L(f"{_sec(gb)} derrière", f"{_sec(gb)} behind", f"arkadaki {_sec(gb)}"))
    if not parts:
        return L("Personne de proche autour de toi.", "Nobody close around you.", "Yakınında kimse yok.")
    return L("Écart : ", "Gaps: ", "Farklar: ") + ", ".join(parts) + "."


def repairs(s: Snapshot, p: Plan) -> str:
    if s.no_repair:
        return L("Tu m'as demandé de ne pas réparer : au prochain arrêt, on ne touche pas à la voiture.",
                 "You asked for no repairs: we won't touch the car at the next stop.",
                 "Onarım istemedin: bir sonraki pit stopta araca dokunmuyoruz.")
    if s.repair:
        return L("Oui, la voiture est abîmée : je prévois les réparations au prochain arrêt. Dis « pas de réparation » si tu n'en veux pas.",
                 "Yes, the car is damaged: I'm planning repairs at the next stop. Say \"no repairs\" if you don't want them.",
                 "Evet, araç hasarlı: bir sonraki pit stopta onarım planlıyorum. İstemiyorsan \"tamir yok\" de.")
    return L("Pas de dégâts importants, pas besoin de réparer.", "No significant damage, no need for repairs.", "Önemli bir hasar yok, onarıma gerek yok.")


def position(s: Snapshot, p: Plan) -> str:
    out = L(f"Tu es P{s.position}", f"You're P{s.position}", f"Sıran P{s.position}")
    if s.class_position and s.class_position != s.position:
        out += L(f", P{s.class_position} dans ta catégorie", f", P{s.class_position} in class", f", sınıfında P{s.class_position}")
    return out + "."


def weather(s: Snapshot, p: Plan) -> str:
    out = L(f"{p.weather or 'Sec'}. Air {_f(s.air_temp, 0)} degrés, piste {_f(s.track_temp, 0)}.",
            f"{p.weather or 'Dry'}. Air {_f(s.air_temp, 0)} degrees, track {_f(s.track_temp, 0)}.",
            f"{p.weather or 'Kuru'}. Hava {_f(s.air_temp, 0)} derece, pist {_f(s.track_temp, 0)}.")
    if p.tyre_compound == "pluie":
        out += L(" Je te conseille les pneus pluie au prochain arrêt.", " I'd go to wets at the next stop.", " Bir sonraki pit stopta yağmur lastiğine geçerdim.")
    elif p.tyre_compound == "slicks":
        out += L(" On peut repasser en slicks.", " We can go back to slicks.", " Slick lastiğe dönebiliriz.")
    return out


def laptimes(s: Snapshot, p: Plan) -> str:
    return L(f"Dernier tour en {_laptime(s.last_lap)}, meilleur en {_laptime(s.best_lap)}.",
             f"Last lap {_laptime(s.last_lap)}, best {_laptime(s.best_lap)}.",
             f"Son tur {_laptime(s.last_lap)}, en iyi tur {_laptime(s.best_lap)}.")


def status(s: Snapshot, p: Plan) -> str:
    return " ".join([position(s, p), laps(s, p), pit(s, p)])


TOPICS = [
    # (mots-clés, fonction)
    (r"essence|des sens|d sens|carbu|fuel|conso|litre|reservoir|jus|gasoil|\bgas\b|petrol|consumption|\btank", fuel),
    (r"energie|batterie|virtuel|energy|battery|virtual", energy),
    (r"stand|\bbox|arret|rentr|\bpit|ravitaill|strateg|\bstop\b|come in", pit),
    (r"pneu|gomme|usure|\btyre|\btire|rubber|\bwear", tyres),
    (r"ecart|devant|derriere|\bgap|attaque|revient|ahead|behind|in front|catching", gaps),
    (r"repar|degat|dommage|abime|damage|repair", repairs),
    (r"position|place|classement|quelle p\b|combien.*(je suis|suis je)|where am i|what p\b", position),
    (r"meteo|pluie|pleut|pleuvoir|mouill|humid|sec\b|temperature|nuage|weather|\brain|\bwet\b|\bdry\b|cloud", weather),
    (r"chrono|dernier tour|meilleur tour|temps au tour|mon tour|rythme|lap time|last lap|best lap|\bpace\b", laptimes),
    (r"combien de tour|tours restant|reste.*tour|temps restant|reste.*minute|\bfin\b|arrivee|damier|how many laps|laps (left|to go|remaining)|time (left|remaining)|finish", laps),
    (r"point complet|situation|resume|tout va|comment ca va|ca donne quoi|recap|update|summary|how are we|status|how s it going", status),
]


LAPTIMES = None  # branché par app.py
BEHAVIOR = None
PIT_ACTION = None   # fonction appelée pour régler le menu des stands
REPAIR_SET = None   # fonction(True/False) : réparer ou pas au prochain arrêt ; renvoie True si le menu est re-réglé
LAST_TEXT = ""      # dernière question (normalisée), pour savoir si on parle de devant ou de derrière


def pit_command(s: Snapshot, p: Plan) -> str:
    if PIT_ACTION is None:
        return L("Je ne peux pas régler le stand pour l'instant.", "I can't set the pit menu right now.", "Şu an pit menüsünü ayarlayamıyorum.")
    PIT_ACTION()
    return L("Je règle le menu des stands.", "Setting up the pit menu.", "Pit menüsünü ayarlıyorum.")


def behavior(s: Snapshot, p: Plan) -> str:
    return BEHAVIOR.summary() if BEHAVIOR else L("Analyse du comportement indisponible.", "Handling analysis unavailable.", "Davranış analizi kullanılamıyor.")


def record(s: Snapshot, p: Plan) -> str:
    return LAPTIMES.record_text(s) if LAPTIMES else L("Pas de chronos enregistrés.", "No lap times recorded.", "Kayıtlı tur zamanı yok.")


def sectors(s: Snapshot, p: Plan) -> str:
    if not LAPTIMES or not LAPTIMES.last:
        return L("Il me faut un tour complet et valide pour comparer les secteurs.", "I need a full valid lap to compare sectors.", "Sektörleri karşılaştırmak için tam ve geçerli bir tur lazım.")
    t = LAPTIMES.summary()
    return t or L("Pas de voiture à comparer autour de toi pour l'instant.", "No car to compare with around you yet.", "Şimdilik çevrende karşılaştıracak araç yok.")


def sectors_ahead(s: Snapshot, p: Plan) -> str:
    t = LAPTIMES.summary("ahead") if LAPTIMES else ""
    return t or L("Pas de comparaison possible avec la voiture devant pour l'instant.", "Can't compare with the car ahead yet.", "Öndeki araçla henüz karşılaştırma yapamıyorum.")


def sectors_behind(s: Snapshot, p: Plan) -> str:
    t = LAPTIMES.summary("behind") if LAPTIMES else ""
    return t or L("Pas de comparaison possible avec la voiture derrière pour l'instant.", "Can't compare with the car behind yet.", "Arkadaki araçla henüz karşılaştırma yapamıyorum.")


def penalty(s: Snapshot, p: Plan) -> str:
    if not s.penalties:
        return L("Aucune pénalité en cours.", "No penalty pending.", "Bekleyen ceza yok.")
    return L(f"Tu as {s.penalties} pénalité{'s' if s.penalties > 1 else ''} à purger aux stands. Regarde l'écran pour le type.",
             f"You have {s.penalties} penalt{'ies' if s.penalties > 1 else 'y'} to serve in the pits. Check the screen for the type.",
             f"Pitte çekmen gereken {s.penalties} cezan var. Türü için ekrana bak.")


TOPICS[:0] = [
    (r"penalit|drive through|stop (and|n|et) go|penalty", penalty),
    (r"(prepare|regle|mets|programme|set up|setup the|program).*(stand|arret|box|menu|pit|stop)|(stand|arret).*(prepare|regle)", pit_command),
    (r"comporte|sous vir|sousvir|survir|equilibr|reglage|setup|conseil|glisse|bloqu|patin|tient pas|braqu|tourne pas|ralenti|pousse|lourde|decroche|chasse|instable|nerveuse|freinage|balance|understeer|oversteer|handling|advice|sliding|locking|wheelspin|loose|snap|braking|won t turn", behavior),
    (r"record|meilleur temps (ici|du circuit)|tour ideal|personal best|ideal lap", record),
    (r"(secteur|perd|gagne|reprend|compar|plus rapide|plus lent|sector|losing|gaining|faster|slower).*(devant|ahead|in front)|(devant|ahead|in front).*(secteur|perd|gagne|rapide|lent|sector|losing|gaining|faster|slower)", sectors_ahead),
    (r"(secteur|perd|gagne|reprend|compar|plus rapide|plus lent|sector|losing|gaining|faster|slower).*(derriere|behind)|(derriere|behind).*(secteur|perd|gagne|rapide|lent|sector|losing|gaining|faster|slower)", sectors_behind),
    (r"secteur|ou je perds|ou je gagne|ou est ce que je perds|compar|sector|where am i losing|where do i lose", sectors),
]


VOICE = None      # pour le mode silence
ENG = None        # pour répéter le dernier message

COMMANDS = [
    (r"\bsilence\b|tais toi|taise|chut\b|moins de message|arrete de parler|mode calme|plus que l urgent|be quiet|shut up|quiet mode|less talk|only urgent", "quiet"),
    (r"reparle|parle a nouveau|tu peux parler|fin du silence|toutes les infos|remets? (la radio|les annonces)|talk again|you can talk|all info|unmute|resume radio", "talk"),
    (r"repete|redis|redire|j ai pas (compris|entendu)|pardon\b|t as dit quoi|qu est ce que tu as dit|repeat|say again|come again|what did you say", "repeat"),
]


def command(text: str) -> str | None:
    """Commandes de radio (silence, reparle, répète) : marchent même hors de la voiture."""
    t = _norm(text)
    for pattern, cmd in COMMANDS:
        if not re.search(pattern, t):
            continue
        if cmd == "quiet" and VOICE is not None:
            VOICE.quiet = True
            return L("Bien reçu, je ne te dis plus que l'urgent. Dis « reparle » pour tout réentendre.",
                     "Copy, urgent calls only from now on. Say \"talk again\" to hear everything.",
                     "Anlaşıldı, artık yalnızca acil çağrılar. Her şeyi duymak için \"tekrar konuş\" de.")
        if cmd == "talk" and VOICE is not None:
            VOICE.quiet = False
            return L("C'est noté, je te redonne toutes les infos.", "Copy, back to full info.", "Anlaşıldı, tüm bilgilere geri dönüyorum.")
        if cmd == "repeat" and ENG is not None:
            last = next((m["text"] for m in ENG.radio if not m.get("me")), None)
            return L(f"Je répète : {last}", f"I repeat: {last}", f"Tekrar ediyorum: {last}") if last else L("Je n'ai encore rien dit.", "I haven't said anything yet.", "Henüz bir şey söylemedim.")
    return None


# ------------------------------------------------------------ discussion
# Petites phrases de radio qui marchent partout, même hors de la voiture.
def _radio_check(s: Snapshot) -> str:
    return L("Oui, je t'entends, cinq sur cinq.", "Yes, I hear you loud and clear.", "Evet, seni net duyuyorum.")


def _here(s: Snapshot) -> str:
    return L("Je suis là, je t'écoute.", "I'm here, go ahead.", "Buradayım, dinliyorum.")


def _hello(s: Snapshot) -> str:
    import voice
    return L(f"Salut ! Je suis là, {'prête' if voice.engineer_female() else 'prêt'} quand tu veux.", "Hi! I'm here, ready when you are.", "Selam! Buradayım, sen hazır olduğunda hazırım.")


def _thanks(s: Snapshot) -> str:
    return L("Avec plaisir.", "You're welcome.", "Rica ederim.")


def _who(s: Snapshot) -> str:
    import voice
    name = voice.engineer_name()
    return L(f"Je suis {name}, ton {'ingénieure' if voice.engineer_female() else 'ingénieur'} de course.",
             f"I'm {name}, your race engineer.",
             f"Ben {name}, yarış mühendisinim.")


def _help(s: Snapshot) -> str:
    return L("Demande-moi l'essence, l'énergie, les pneus, le stand, les écarts, ta position, la météo, tes chronos, "
             "tes secteurs ou le comportement de la voiture. Je peux aussi régler le menu des stands.",
             "Ask me about fuel, energy, tyres, pit stop, gaps, position, weather, lap times, sectors "
             "or car handling. I can also set up the pit menu.",
             "Bana yakıtı, enerjiyi, lastikleri, pit stopu, farkları, pozisyonunu, hava durumunu, tur zamanlarını, sektörleri " "ya da aracın davranışını sorabilirsin. Pit menüsünü de ayarlayabilirim.")


def _time(s: Snapshot) -> str:
    import datetime
    n = datetime.datetime.now()
    h = "heure" if n.hour <= 1 else "heures"
    return L(f"Il est {n.hour} {h} {n.minute:02d}." if n.minute else f"Il est {n.hour} {h}.", f"It's {n:%H:%M}.", f"Saat {n:%H:%M}.")


def _how_are_you(s: Snapshot) -> str | None:
    if s.on_track:   # en piste, « ça va ? » = le point complet (sujet status)
        return None
    import voice
    return L(f"Ça va très bien, merci ! {'Prête' if voice.engineer_female() else 'Prêt'} pour la course.", "All good, thanks! Ready for the race.", "Çok iyiyim, teşekkürler! Yarışa hazırım.")


def _car(s: Snapshot) -> str:
    from alerts import car_sentence
    return car_sentence(s.car_class, s.car)


def _joke(s: Snapshot) -> str:
    return L("Les blagues, c'est après le drapeau à damier. Reste concentré !",
             "Jokes are for after the chequered flag. Stay focused!",
             "Şakalar damalı bayraktan sonra. Odaklan!")


CHAT = [
    (r"\bm entends?\b|\bm entendez\b|tu me recois|me recois tu|tu recois|radio check|test radio|\ballo\b|"
     r"can you hear|do you hear|hear me|do you copy|you copy", _radio_check),
    (r"\b(t es|tu es|es tu|t es bien) la\b|t es toujours la|you there|are you there", _here),
    (r"^(bonjour|salut|hello|coucou|hey|bonsoir|hi)\b", _hello),
    (r"\bmerci\b|thank", _thanks),
    (r"qui es tu|t es qui|tu es qui|comment tu t appelles|ton nom|ton prenom|who are you|your name", _who),
    (r"que (peux|sais) tu|qu est ce que tu (peux|sais)|tu sais faire|tu peux faire quoi|\baide\b|\bhelp\b|what can you", _help),
    (r"quelle heure|l heure qu il|what time", _time),
    (r"\bca va\b|comment vas tu|how are you", _how_are_you),
    (r"blague|joke|funny", _joke),
    (r"\belms\b|\be l m s\b|\bwec\b|\bw e c\b|quelle voiture|quelle categorie|quel championnat|"
     r"which car|what car|which class|which championship", _car),
]


# ------------------------------------------------------------ turc
# Mots-clés turcs (sans accents, « ı » -> « i »), ajoutés aux listes ci-dessus sans y toucher.
_TR_TOPICS = {
    penalty: r"\bceza",
    pit_command: r"(\bpit|\bboks|menu).*(ayarla|hazirla|programla)|(ayarla|hazirla).*(\bpit|\bboks|menu)",
    behavior: r"davranis|onden kay|arkadan kay|\bdenge|\bayar(?!la)|tavsiye|\boneri|kayiyor|kilitlen|patinaj|donmuyor|savrul|frenleme",
    record: r"rekor|en iyi zaman|ideal tur",
    sectors_ahead: r"(sektor|kaybed|kazan|karsilastir|daha hizli|daha yavas).*(ondeki|onumde)|(ondeki|onumde).*(sektor|kaybed|kazan|hizli|yavas)",
    sectors_behind: r"(sektor|kaybed|kazan|karsilastir|daha hizli|daha yavas).*(arkadaki|arkamda)|(arkadaki|arkamda).*(sektor|kaybed|kazan|hizli|yavas)",
    sectors: r"sektor|nerede kaybed|nerede kazan|karsilastir",
    fuel: r"yakit|benzin|\bdepo|tuketim",
    energy: r"enerji|batarya|\bsanal",
    pit: r"\bboks|strateji|\bdurus|ikmal|ne zaman gir",
    tyres: r"lastik|asinma|\bteker",
    gaps: r"\bfark|aralik|mesafe|ondeki|onumde|arkadaki|arkamda|yaklasiyor",
    repairs: r"tamir|hasar|\bonar",
    position: r"pozisyon|kacinci|\bsiralama|\bsiram\b|\bsirada",
    weather: r"\bhava|yagmur|yagacak|yagiyor|\bislak|\bkuru\b|sicaklik|bulut",
    laptimes: r"tur zaman|son tur|en iyi tur|tur suresi|\btempo",
    laps: r"kac tur|kalan tur|tur kaldi|kalan sure|sure kaldi|ne kadar kaldi|dakika kaldi|\bbitis|damali",
    status: r"genel durum|durum ne|\bozet|\brapor|nasil gidiyor|her sey yolunda",
}
_TR_COMMANDS = {
    "quiet": r"sessiz moda|sessiz modu ac|sessiz ol|\bsus\b|\bkonusma\b|sadece acil|daha az konus",
    "talk": r"tekrar konus|konusabilirsin|konusmaya devam|sessizligi kapat|sessiz modu kapat|tum bilgi",
    "repeat": r"tekrarla|tekrar et|tekrar soyle|bir daha soyle|anlamadim|duymadim|ne dedin|\befendim\b",
}
_TR_CHAT = {
    _radio_check: r"duyuyor musun|beni duy|sesim geliyor|telsiz kontrol|radyo kontrol|\balo\b",
    _here: r"orada misin|burada misin|hala orada",
    _hello: r"^(merhaba|selam|gunaydin|iyi aksamlar)\b",
    _thanks: r"tesekkur|sagol|sag ol|eyvallah",
    _who: r"kimsin|adin ne|ismin ne",
    _help: r"ne yapabilirsin|neler yapabilirsin|\byardim",
    _time: r"saat kac",
    _how_are_you: r"nasilsin|\bnaber\b|ne haber",
    _joke: r"\bsaka\b|fikra",
    _car: r"hangi araba|hangi arac|hangi sinif|hangi kategori|hangi sampiyona",
}
TOPICS = [(f"{p}|{_TR_TOPICS[fn]}" if fn in _TR_TOPICS else p, fn) for p, fn in TOPICS]
COMMANDS = [(f"{p}|{_TR_COMMANDS[c]}" if c in _TR_COMMANDS else p, c) for p, c in COMMANDS]
CHAT = [(f"{p}|{_TR_CHAT[fn]}" if fn in _TR_CHAT else p, fn) for p, fn in CHAT]


FILLER = (_hello, _thanks)   # « salut », « merci » : seulement si la phrase ne demande rien d'autre


def chat(t: str, s: Snapshot, alone: bool = True) -> list[str]:
    """alone=False : la phrase demande aussi des données, on saute « salut » / « merci »."""
    out = []
    for pattern, fn in CHAT:
        if re.search(pattern, t) and (alone or fn not in FILLER):
            r = fn(s)
            if r and r not in out:
                out.append(r)
    return out


NO_REPAIR = (r"\b(pas|plus|sans|aucune?|annule|enleve|leve|retire|supprime|oublie|non)\b.*\brepar|"
             r"\brepar\w*\b.*\b(non|pas|enleve|annule|retire|supprime)\b|\bne repare|"
             r"don t (fix|repair)|no repairs?\b|skip (the )?repairs?|cancel (the )?repairs?|"
             r"\b(remove|no) (the )?(repairs?|damage)|"
             r"tamir (etme|yok|yapma|istemiyorum)|tamiri (iptal|kaldir)|\bonarma\b|onarim (yok|yapma|istemiyorum)|tamirsiz")
DO_REPAIR = (r"\b(repare|reparer|reparez|reparation|reparations)\b|\bfix (the )?(car|damage)|\brepair|"
             r"tamir (et|edin|edelim)\b|\bonar(in|alim)?\b")


def repair_command(t: str) -> str | None:
    """« pas de réparation » / « répare la voiture » : change le menu des stands."""
    if re.search(NO_REPAIR, t):
        want = False
    elif re.search(DO_REPAIR, t) and re.search(r"\b(repare|reparer|reparez|tout|carrosserie|fix|repair the|do the|on repare|tamir et|tamir edin|tamir edelim|onar|onarin|onaralim|hepsini|kaporta)\b", t):
        want = True
    else:
        return None
    if REPAIR_SET is None:
        return L("Je ne peux pas changer les réparations pour l'instant.", "I can't change the repairs right now.", "Şu an onarımları değiştiremiyorum.")
    body_only = want and bool(re.search(r"carrosserie|body|aero|kaporta|govde", t)) and not re.search(r"\btout\b|\ball\b|full|hepsi|her sey|tamamen|tumunu", t)
    menu = REPAIR_SET(want, not body_only)
    if not want:
        out = L("C'est noté, on ne répare pas au prochain arrêt.", "Copy, no repairs at the next stop.", "Anlaşıldı, bir sonraki pit stopta onarım yok.")
    elif body_only:
        out = L("C'est noté, on répare seulement la carrosserie au prochain arrêt.", "Copy, bodywork only at the next stop.", "Anlaşıldı, bir sonraki pit stopta yalnızca kaporta.")
    else:
        out = L("C'est noté, on répare tout au prochain arrêt.", "Copy, full repairs at the next stop.", "Anlaşıldı, bir sonraki pit stopta tam onarım.")
    if menu:
        out += L(" Je corrige le menu des stands.", " Updating the pit menu.", " Pit menüsünü güncelliyorum.")
    return out


def answer(text: str, s: Snapshot, p: Plan) -> str:
    global LAST_TEXT
    c = command(text)
    if c:
        return c
    t = _norm(text).strip()
    if not t:
        return L("Je n'ai rien entendu, répète.", "I didn't hear anything, say again.", "Hiçbir şey duymadım, tekrarla.")
    LAST_TEXT = t
    r = repair_command(t)
    if r:
        return r
    if not s.on_track:
        # hors piste, « comment ça va » est une politesse, pas une demande de point complet
        polite = bool(re.search(r"\bca va\b|comment vas tu|how are you|nasilsin|\bnaber\b|ne haber", t))
        asks_data = any(re.search(pattern, t) for pattern, fn in TOPICS if not (polite and fn is status))
        said = chat(t, s, alone=not asks_data)
        if asks_data:
            said.append(L("Je n'ai pas encore de données, monte dans la voiture.", "No data yet, get in the car.", "Henüz veri yok, araca bin."))
        if said:
            return " ".join(said)
        return L("Je t'entends, mais je n'ai pas compris. Une fois en piste, demande-moi l'essence, les pneus, "
                 "le stand, les écarts, la position ou la météo.",
                 "I hear you, but I didn't get that. Once you're on track, ask me about fuel, tyres, "
                 "pit stop, gaps, position or weather.",
                 "Seni duyuyorum ama anlamadım. Piste çıkınca bana yakıtı, lastikleri, " "pit stopu, farkları, pozisyonu ya da hava durumunu sor.")
    said = chat(t, s, alone=not any(re.search(pattern, t) for pattern, _ in TOPICS))
    replies = []
    for pattern, fn in TOPICS:
        if re.search(pattern, t) and fn not in [r[0] for r in replies]:
            if fn is gaps and any(r[0] in (sectors, sectors_ahead, sectors_behind) for r in replies):
                continue
            if fn is sectors and any(r[0] in (sectors_ahead, sectors_behind) for r in replies):
                continue
            if fn is laptimes and any(r[0] is record for r in replies):
                continue
            replies.append((fn, fn(s, p)))
    if not replies and not said:
        # jamais sans réponse : on donne le point complet
        return L("Pas sûr d'avoir compris, voilà le point. ", "Not sure I got that, here's the update. ", "Tam anladığımdan emin değilim, işte genel durum. ") + status(s, p)
    return " ".join(said + [r[1] for r in replies[:3]])
