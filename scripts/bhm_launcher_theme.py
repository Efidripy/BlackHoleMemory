"""Qt stylesheet and palette for the BHM launcher."""

COLOR_BG = "#0F111A"
COLOR_PANEL = "#121622"
COLOR_CARD = "#161925"
COLOR_CARD_2 = "#101421"
COLOR_BORDER = "#2A2E3D"
COLOR_MUTED = "#8B95AA"
COLOR_TEXT = "#F2F5FF"
COLOR_CYAN = "#00E5FF"
COLOR_PINK = "#FF4081"
COLOR_GREEN = "#00E676"
COLOR_YELLOW = "#FFD54F"
COLOR_RED = "#FF5252"

def build_qss() -> str:
    return f"""
    * {{
        font-family: "Segoe UI", "Inter", Arial, sans-serif;
        color: {COLOR_TEXT};
        letter-spacing: 0px;
    }}
    QWidget#SetupScreen, QWidget#DashboardScreen, QWidget#IntegrationsOnboarding, QWidget#IntegrationsPanel {{
        background: {COLOR_BG};
    }}
    QFrame#Sidebar, QFrame#MainPanel, QFrame#OperatorDrawer {{
        background: {COLOR_PANEL};
        border: 1px solid {COLOR_BORDER};
        border-radius: 12px;
    }}
    QFrame#Card, QFrame#ServiceCard, QFrame#LinkCard, QFrame#MetricCard, QFrame#LogsWindow {{
        background: {COLOR_CARD};
        border: 1px solid {COLOR_BORDER};
        border-radius: 8px;
    }}
    QFrame#OperatorCard {{
        background: #101622;
        border: 1px solid #263145;
        border-radius: 8px;
    }}
    QScrollArea#OperatorScroll, QWidget#OperatorDrawerContent {{
        background: transparent;
        border: none;
    }}
    QFrame#Card {{
        min-height: 150px;
    }}
    QFrame#Card:hover, QFrame#ServiceCard:hover, QFrame#LinkCard:hover, QFrame#MetricCard:hover {{
        border-color: {COLOR_CYAN};
        background: #182032;
    }}
    QLabel#HeroTitle {{
        color: #FFFFFF;
        font-size: 38px;
        font-weight: 850;
    }}
    QLabel#SidebarTitle {{
        color: #FFFFFF;
        font-size: 25px;
        font-weight: 850;
    }}
    QLabel#SectionTitle, QLabel#CardTitle {{
        color: #F8FAFF;
        font-size: 18px;
        font-weight: 800;
    }}
    QLabel#Muted, QLabel#MutedLarge {{
        color: {COLOR_MUTED};
        font-size: 13px;
    }}
    QLabel#MutedLarge {{
        font-size: 15px;
    }}
    QLabel#DependencyName, QLabel#LinkTitle {{
        color: #F3F6FF;
        font-size: 14px;
        font-weight: 750;
    }}
    QLabel#MetricTitle {{
        color: {COLOR_MUTED};
        font-size: 11px;
        font-weight: 800;
    }}
    QLabel#MetricValue {{
        color: {COLOR_CYAN};
        font-size: 30px;
        font-weight: 900;
    }}
    QLabel#VersionLabel {{
        color: {COLOR_MUTED};
        font-size: 10px;
        font-weight: 400;
    }}
    QLabel#OperatorDrawerEmpty {{
        color: {COLOR_MUTED};
        background: {COLOR_CARD_2};
        border: 1px dashed #34405A;
        border-radius: 8px;
        padding: 14px;
        font-size: 12px;
    }}
    QLabel#OperatorTitle {{
        color: #FFFFFF;
        font-size: 15px;
        font-weight: 900;
    }}
    QLabel#OperatorIntro {{
        color: {COLOR_MUTED};
        font-size: 12px;
    }}
    QLabel#OperatorGroupTitle {{
        color: {COLOR_CYAN};
        font-size: 11px;
        font-weight: 900;
        padding: 3px 2px 1px 2px;
    }}
    QLabel#OperatorHint {{
        color: {COLOR_MUTED};
        font-size: 12px;
    }}
    QLabel#FieldLabel {{
        color: {COLOR_CYAN};
        font-size: 11px;
        font-weight: 850;
    }}
    QLabel#Mono {{
        color: #B9C4D8;
        background: #0B0F19;
        border: 1px solid #263145;
        border-radius: 7px;
        padding: 8px 10px;
        font-family: "Cascadia Mono", "Consolas", monospace;
        font-size: 11px;
    }}
    QLabel#LinkTag {{
        color: {COLOR_CYAN};
        font-size: 11px;
        font-weight: 850;
    }}
    QLabel#PillOk, QLabel#PillMissing, StatusBadge {{
        border-radius: 7px;
        padding: 6px 10px;
        font-size: 12px;
        font-weight: 850;
    }}
    QLabel#PillOk, StatusBadge[state="running"] {{
        color: {COLOR_GREEN};
        background: #09291F;
        border: 1px solid #0F6B4F;
    }}
    QLabel#PillMissing, StatusBadge[state="stopped"], StatusBadge[state="error"] {{
        color: {COLOR_RED};
        background: #33131C;
        border: 1px solid #7A2638;
    }}
    StatusBadge[state="checking"] {{
        color: {COLOR_YELLOW};
        background: #30260B;
        border: 1px solid #826819;
    }}
    QPushButton {{
        background: #1A2030;
        color: #EAF0FF;
        border: 1px solid #30384B;
        border-radius: 8px;
        padding: 7px 12px;
        font-size: 12px;
        font-weight: 750;
    }}
    QPushButton:hover {{
        background: #222A3D;
        border-color: {COLOR_CYAN};
    }}
    QPushButton#PrimaryButton {{
        background: #063B38;
        color: #F8FFFF;
        border: 1px solid {COLOR_CYAN};
        padding: 15px 18px;
        font-size: 15px;
        font-weight: 900;
    }}
    QPushButton#PrimaryButton:hover {{
        background: #09504B;
        border-color: {COLOR_GREEN};
    }}
    QPushButton#GhostButton {{
        background: #111722;
        color: #B9C4D8;
        border-color: #263145;
    }}
    QPushButton#GhostButton:hover {{
        background: #172133;
        color: #FFFFFF;
        border-color: {COLOR_CYAN};
    }}
    QPushButton#OperatorDrawerToggle {{
        background: #111722;
        color: {COLOR_CYAN};
        border: 1px solid #263145;
        border-radius: 8px;
        padding: 9px 5px;
        font-size: 10px;
        font-weight: 900;
    }}
    QPushButton#OperatorDrawerToggle:hover, QPushButton#OperatorDrawerToggle:checked {{
        background: #172133;
        color: #FFFFFF;
        border-color: {COLOR_CYAN};
    }}
    QPushButton#OperatorActionButton, QPushButton#OperatorDangerButton {{
        min-width: 0px;
        padding: 4px 10px;
        font-size: 12px;
        font-weight: 800;
        border-radius: 7px;
    }}
    QPushButton#OperatorActionButton {{
        background: #111A28;
        color: #DCE7FA;
        border: 1px solid #2A3950;
    }}
    QPushButton#OperatorActionButton:hover {{
        background: #18263A;
        color: #FFFFFF;
        border-color: {COLOR_CYAN};
    }}
    QPushButton#OperatorDangerButton {{
        background: #2B1720;
        color: #FFDCE9;
        border: 1px solid #6C3046;
    }}
    QPushButton#OperatorDangerButton:hover {{
        background: #3D1B29;
        border-color: {COLOR_PINK};
    }}
    QScrollArea#OperatorScroll QScrollBar:vertical {{
        background: #0D131E;
        width: 9px;
        margin: 2px 0px;
        border: none;
        border-radius: 4px;
    }}
    QScrollArea#OperatorScroll QScrollBar::handle:vertical {{
        background: #34445E;
        min-height: 30px;
        border-radius: 4px;
    }}
    QScrollArea#OperatorScroll QScrollBar::handle:vertical:hover {{
        background: {COLOR_CYAN};
    }}
    QScrollArea#OperatorScroll QScrollBar::add-line:vertical,
    QScrollArea#OperatorScroll QScrollBar::sub-line:vertical {{
        height: 0px;
    }}
    QScrollArea#OperatorScroll QScrollBar::add-page:vertical,
    QScrollArea#OperatorScroll QScrollBar::sub-page:vertical {{
        background: transparent;
    }}
    QPushButton#AccentButton {{
        background: #063B38;
        color: {COLOR_GREEN};
        border-color: #0D735F;
    }}
    QPushButton#AccentButton:hover {{
        background: #0B5048;
        border-color: {COLOR_GREEN};
    }}
    QPushButton#DangerButton {{
        background: #481722;
        color: #FFEAF1;
        border-color: {COLOR_PINK};
    }}
    QPushButton#DangerButton:hover {{
        background: #641D2F;
        border-color: #FF6EA5;
    }}
    QPushButton#ModeToggle {{
        background: #101827;
        color: {COLOR_CYAN};
        border: 1px solid #263145;
        border-radius: 7px;
        padding: 1px 7px;
        font-size: 10px;
        font-weight: 850;
    }}
    QPushButton#ModeToggle:hover {{
        background: #142233;
        border-color: {COLOR_CYAN};
        color: #FFFFFF;
    }}
    QProgressBar {{
        background: #0B0F19;
        border: 1px solid {COLOR_BORDER};
        border-radius: 8px;
        color: #F8FAFF;
        min-height: 24px;
        text-align: center;
    }}
    QProgressBar::chunk {{
        background: {COLOR_CYAN};
        border-radius: 7px;
    }}
    QTextEdit#Console {{
        background: #070A10;
        border: 1px solid {COLOR_BORDER};
        border-radius: 8px;
        color: #DDE5F5;
        font-family: "Cascadia Mono", "Consolas", monospace;
        font-size: 11px;
        padding: 10px;
    }}
    QLineEdit, QComboBox {{
        background: {COLOR_CARD_2};
        border: 1px solid #30384B;
        border-radius: 7px;
        padding: 6px 10px;
        color: {COLOR_TEXT};
        font-size: 12px;
        font-weight: 700;
    }}
    QComboBox#LogFilter {{
        padding: 5px 28px 5px 10px;
        min-width: 132px;
    }}
    QComboBox#LogFilter::drop-down {{
        subcontrol-origin: padding;
        subcontrol-position: top right;
        width: 24px;
        border-left: 1px solid #30384B;
    }}
    QComboBox#LogFilter::down-arrow {{
        width: 9px;
        height: 9px;
    }}
    QLineEdit#LlmInput {{
        background: #1A1D27;
        border: 1px solid #343B50;
        border-radius: 7px;
        padding: 6px 9px;
        color: #F2F5FF;
        font-size: 12px;
        font-weight: 750;
    }}
    QLineEdit#LlmInput:focus {{
        border-color: {COLOR_CYAN};
        background: #1D2330;
    }}
    QFrame#LlmConfig {{
        background: transparent;
        border: 0;
    }}
    QMenu {{
        background: {COLOR_CARD};
        border: 1px solid {COLOR_BORDER};
        padding: 6px;
    }}
    QMenu::item {{
        padding: 8px 22px;
        border-radius: 6px;
    }}
    QMenu::item:selected {{
        background: #243047;
        color: {COLOR_CYAN};
    }}
    """

