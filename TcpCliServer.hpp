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
// requires the client to authenticate before issuing commands.
//
// States:
//   Unauthed  — connection just opened, awaiting "auth <password>" if required
//   Idle      — authenticated, accepts set/status/run/… commands
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
    Q_SLOT void onDecodes(QStringList decodes);
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

    // Fires for "set rxfreq <Hz>" / select
    Q_SIGNAL void setRxAudioFreqSignal(int hz);

    // Fires for "set txfirst on|off"
    Q_SIGNAL void setTxFirstSignal(bool txFirst);

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
    void printHelp();
    void printStatus();
    void printSelectedSlot();

    // Spectrum rendering
    QString renderSpectrum(QVector<float> const& savg, float df3, int nfa, int nfb, int selectedHz, bool txFirst) const;

    void appendCliLog(QString const& role, QString const& text);

    QTcpServer    m_server;
    QTcpSocket*   m_client {nullptr};
    State         m_state  {State::Unauthed};

    QString       m_password;           // empty → no auth required
    QString       m_readBuf;            // line accumulation buffer

    static constexpr int m_spectrumWidth = 96;
    QStringList   m_lastDecodes;        // last full decode batch

    // Selection is keyed by audio frequency (Hz), not by decode-list index.
    // m_selectedDecode empty → freq set but no decode there (valid for cq)
    int           m_selectedFreq   {1200};
    QString       m_selectedDecode;     // raw decode string at m_selectedFreq, or ""

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

    QFile          m_cliLog;          // append-only transcript: exe dir / wsjtcb-cli.log
};

#endif // TCP_CLI_SERVER_HPP
