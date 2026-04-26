#ifndef TCP_CLI_SERVER_HPP
#define TCP_CLI_SERVER_HPP

#include <QObject>
#include <QFile>
#include <QHostAddress>
#include <QTcpServer>
#include <QTcpSocket>
#include <QStringList>
#include <QTime>
#include <QVector>

//
// TcpCliServer — single-client TCP CLI for WSJT-CB.
//
// Start-up: pass --cli-port <N> to enable; optional --cli-pass <password>
// and --cli-bind <address> (default 0.0.0.0).
// If --cli-pass is set, the client must send that password as the first line;
// no banner or prompt is sent until it matches. Otherwise the connection is Idle immediately.
//
// States:
//   Unauthed  — --cli-pass set; only "? " has been sent; awaiting first line (password) before banner
//   Idle      — ready for commands (no password, or password line matched)
//   Running   — streaming mode: pushes one spectrum + decode burst per TR period
//
// TX signals are wired externally (in MainWindow::connectCli) to MainWindow's
// private replyToCQ() and on_txb6_clicked() slots.
//

class TcpCliServer : public QObject
{
    Q_OBJECT

public:
    explicit TcpCliServer(quint16 port, QString const& password,
                         QHostAddress bindAddress = QHostAddress::AnyIPv4,
                         QObject* parent = nullptr);
    ~TcpCliServer() override;

    bool isListening() const;

    // -----------------------------------------------------------------------
    // Inbound data from MainWindow
    // -----------------------------------------------------------------------
    Q_SLOT void onSpectrum(QVector<float> savg, float df3, int nfa, int nfb);
    // countries[i] is display name for decodes[i] (DXCC entity from DE call); may be shorter than decodes.
    Q_SLOT void onDecodes(QStringList decodes, QStringList countries);
    Q_SLOT void onTxStart(QString message);
    Q_SLOT void onTxStop();
    Q_SLOT void onTxFirstChanged(bool txFirst);
    Q_SLOT void setStationSnapshot(QString const& callsign, QString const& grid);

    // -----------------------------------------------------------------------
    // Outbound: TX actions
    // Wired by MainWindow::connectCli() to private MainWindow slots.
    // -----------------------------------------------------------------------

    // Fires when the user types "answer" — mirrors MessageClient::reply
    Q_SIGNAL void replySignal(QTime time, qint32 snr, float deltaTime,
                              quint32 deltaFreq, QString const& mode,
                              QString const& messageText,
                              bool lowConfidence, quint8 modifiers);

    // Fires when the user types "cq" — set TX audio offset first, then trigger CQ
    Q_SIGNAL void setTxAudioFreqSignal(int hz);
    Q_SIGNAL void startCQSignal();

    // Fires when the CLI sets the station callsign / grid
    Q_SIGNAL void setCallsignSignal(QString callsign);
    Q_SIGNAL void setGridSignal(QString grid);

    // Fires for "stoptx" — same as main-window Stop Tx (halt RF + end AutoSeq until next cq/answer)
    Q_SIGNAL void stopTxSignal();

    // Fires when the TCP client disconnects (used to halt TX if a sequence was started via CLI)
    Q_SIGNAL void clientDisconnected();

    // Fires for "set rxfreq <Hz>" / select
    Q_SIGNAL void setRxAudioFreqSignal(int hz);

    // Fires for "set txfirst on|off"
    Q_SIGNAL void setTxFirstSignal(bool txFirst);

    // Emitted for each line written to the CLI transcript (same text as the log file line, without trailing newline).
    Q_SIGNAL void cliLogLineAppended(QString const& line);

    QString cliLogFilePath() const { return m_cliLogPath; }

private Q_SLOTS:
    void onNewConnection();
    void onClientDisconnected();
    void onReadyRead();

private:
    enum class State { Unauthed, Idle };

    void processLine(QString const& line);
    void handleSet(QStringList const& parts);

    // Select audio Hz, update TX/RX, refresh m_selectedDecode from m_lastDecodes.
    // Sends ERR/OK lines; returns false if selection failed.
    bool trySelectAudioFreq(int freqHz);

    void sendLine(QString const& text);
    // CR+LF so async lines appear below the `> ` prompt (terminal has no echo newline)
    void sendLineAfterNewline(QString const& text);
    void sendPrompt();
    void sendWelcomeBanner();
    void tryFirstLinePassword(QString const& line);
    void printHelp();
    void printStatus();
    void printSelectedSlot();

    // Spectrum: bar + optional marker (two client lines, two log lines — no embedded CR/LF)
    void renderSpectrumLines(QVector<float> const& savg, float df3, int nfa, int nfb, int selectedHz, bool txFirst,
                             QString& barLine, QString& markerLine) const;

    void appendSingleCliLogLine(QString const& role, QString const& text);
    void appendCliLog(QString const& role, QString const& text);

    QTcpServer    m_server;
    QTcpSocket*   m_client {nullptr};
    State         m_state  {State::Unauthed};

    QString       m_password;           // empty → no auth required
    QString       m_readBuf;            // line accumulation buffer

    static constexpr int m_spectrumWidth = 48;
    QStringList   m_lastDecodes;        // last full decode batch
    QStringList   m_lastDecodeCountries; // parallel: DXCC-style country name for CLI (same order as m_lastDecodes)

    // Selection is keyed by audio frequency (Hz), not by decode-list index.
    // m_selectedDecode empty → freq set but no decode there (valid for cq)
    int           m_selectedFreq   {1200};
    QString       m_selectedDecode;     // raw decode string at m_selectedFreq, or ""
    QString       m_selectedCountry;    // country column for m_selectedDecode (CLI display)

    // Last spectrum snapshot (stored so we can send with decode burst)
    QVector<float> m_pendingSpectrum;
    float          m_pendingDf3  {0.f};
    int            m_pendingNfa  {0};
    int            m_pendingNfb  {2700};

    bool           m_spectrumPending {false};
    bool           m_txFirst         {false}; // mirrors MainWindow::m_txFirst

    QString        m_myCallsign;      // mirrored from MainWindow / config (for status)
    QString        m_myGrid;

    QString        m_lastTxMessage;   // last message passed to onTxStart (for TX STOP line)

    QString        m_cliLogPath;     // same path as m_cliLog.fileName() after open
    QFile          m_cliLog;         // append-only transcript: exe dir / wsjtcb-cli.log
};

#endif // TCP_CLI_SERVER_HPP
