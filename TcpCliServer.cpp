#include "TcpCliServer.hpp"

#include <QHostAddress>
#include <QDateTime>
#include <QCryptographicHash>
#include <QVector>
#include <QtMath>
#include <algorithm>
#include <cmath>

// ---------------------------------------------------------------------------
// Construction / listen
// ---------------------------------------------------------------------------

TcpCliServer::TcpCliServer(quint16 port, QString const& password,
                           QHostAddress bindAddress, QObject* parent)
    : QObject(parent)
    , m_password(password)
{
    connect(&m_server, &QTcpServer::newConnection,
            this,      &TcpCliServer::onNewConnection);

    if (!m_server.listen(bindAddress, port))
    {
        qWarning("TcpCliServer: failed to listen on %s:%d: %s",
                 qPrintable(bindAddress.toString()), port,
                 qPrintable(m_server.errorString()));
    }
    else
    {
        qInfo("TcpCliServer: listening on %s:%d",
              qPrintable(bindAddress.toString()), port);
    }
}

bool TcpCliServer::isListening() const
{
    return m_server.isListening();
}

// ---------------------------------------------------------------------------
// Connection management
// ---------------------------------------------------------------------------

void TcpCliServer::onNewConnection()
{
    QTcpSocket* incoming = m_server.nextPendingConnection();

    if (m_client && m_client->state() == QAbstractSocket::ConnectedState)
    {
        // Reject second connection
        incoming->write("ERR: already connected\r\n");
        incoming->flush();
        incoming->disconnectFromHost();
        incoming->deleteLater();
        return;
    }

    m_client = incoming;
    m_state   = m_password.isEmpty() ? State::Idle : State::Unauthed;
    m_readBuf.clear();
    m_selectedFreq = 1200;
    m_selectedDecode.clear();

    connect(m_client, &QTcpSocket::disconnected,
            this,     &TcpCliServer::onClientDisconnected);
    connect(m_client, &QTcpSocket::readyRead,
            this,     &TcpCliServer::onReadyRead);

    sendLine("WSJT-CB CLI ready");
    if (m_state == State::Unauthed)
        sendLine("AUTH required — type: auth <password>");
    else
        sendLine("Type 'help' for commands");
    sendPrompt();
}

void TcpCliServer::onClientDisconnected()
{
    if (m_client)
    {
        m_client->deleteLater();
        m_client = nullptr;
    }
    m_state        = State::Unauthed;
    m_selectedFreq = 1200;
    m_selectedDecode.clear();
    m_readBuf.clear();
}

// ---------------------------------------------------------------------------
// Read loop
// ---------------------------------------------------------------------------

void TcpCliServer::onReadyRead()
{
    if (!m_client) return;

    m_readBuf += QString::fromUtf8(m_client->readAll());

    while (true)
    {
        int idx = m_readBuf.indexOf('\n');
        if (idx < 0) break;

        QString line = m_readBuf.left(idx).trimmed();
        m_readBuf    = m_readBuf.mid(idx + 1);

        if (!line.isEmpty())
            processLine(line);
    }
}

// ---------------------------------------------------------------------------
// Decode string helpers
// ---------------------------------------------------------------------------

// Raw decode format (space-separated, skip empties):
//   [0] time (HHMM or HHMMSS)
//   [1] snr
//   [2] dt
//   [3] audio-freq Hz
//   [4] mode
//   [5..] message words
//
// Returns the audio frequency field as an integer, or -1 on parse failure.
static int extractFreqFromDecode(QString const& raw)
{
    QStringList p = raw.trimmed().split(' ', Qt::SkipEmptyParts);
    if (p.size() < 4) return -1;
    bool ok;
    int f = p[3].toInt(&ok);
    return ok ? f : -1;
}

// Returns "[FREQ] HHMM snr dt mode message" — freq as bracketed prefix,
// removed from its original position so it is not shown twice.
static QString formatDecodeForDisplay(QString const& raw)
{
    QStringList p = raw.trimmed().split(' ', Qt::SkipEmptyParts);
    if (p.size() < 5)
        return raw.trimmed();

    // p[3] is freq — use as prefix, skip in the rejoined string
    QString freq = p[3];
    QStringList body;
    body << p[1] << p[2] << p[4];     // :ss snr dt mode
    if (p.size() > 5)
        body << p.mid(5);                     // message words
    return QString("[%1] %2").arg(freq).arg(body.join("  "));
}

// ---------------------------------------------------------------------------
// Command dispatch
// ---------------------------------------------------------------------------

void TcpCliServer::processLine(QString const& line)
{
    QStringList parts = line.split(' ', Qt::SkipEmptyParts);
    if (parts.isEmpty()) return;

    QString cmd = parts[0].toLower();

    // -----------------------------------------------------------------------
    // Auth gate
    // -----------------------------------------------------------------------
    if (m_state == State::Unauthed)
    {
        if (cmd != "auth")
        {
            sendLine("ERR: not authenticated — type: auth <password>");
            sendPrompt();
            return;
        }

        QString supplied = parts.size() > 1 ? parts[1] : QString{};

        // Constant-time compare to resist timing attacks
        auto h = [](QString const& s) {
            return QCryptographicHash::hash(s.toUtf8(), QCryptographicHash::Sha256);
        };
        bool ok = (h(supplied) == h(m_password));

        if (ok)
        {
            m_state = State::Idle;
            sendLine("OK: authenticated");
            sendLine("Type 'help' for commands");
        }
        else
        {
            sendLine("ERR: wrong password");
        }
        sendPrompt();
        return;
    }

    // -----------------------------------------------------------------------
    // Authenticated commands
    // -----------------------------------------------------------------------

    if (cmd == "help")
    {
        printHelp();
    }
    else if (cmd == "quit" || cmd == "exit")
    {
        sendLine("BYE");
        m_client->flush();
        m_client->disconnectFromHost();
    }
    else if (cmd == "status")
    {
        printStatus();
    }
    else if (cmd == "set" && parts.size() >= 3)
    {
        handleSet(parts);
    }
    else if (cmd == "set")
    {
        sendLine("ERR: usage: set <key> <value>");
    }

    else if (cmd == "select")
    {
        if (parts.size() < 2)
        {
            sendLine("ERR: usage: select <audio-freq-Hz>");
        }
        else
        {
            bool ok;
            int freqHz = parts[1].toInt(&ok);
            if (!ok || freqHz <= 0)
            {
                sendLine("ERR: usage: select <audio-freq-Hz>  (positive integer, e.g. 987)");
            }
            else if ((m_pendingNfa > 0 || m_pendingNfb > 0) &&
                     (freqHz < m_pendingNfa || freqHz > m_pendingNfb))
            {
                sendLine(QString("ERR: %1 Hz is outside active window (%2\xe2\x80\x93%3 Hz)")
                         .arg(freqHz).arg(m_pendingNfa).arg(m_pendingNfb));
            }
            else
            {
                m_selectedFreq = freqHz;
                m_selectedDecode.clear();
                for (auto const& raw : m_lastDecodes)
                {
                    if (extractFreqFromDecode(raw) == freqHz)
                    {
                        m_selectedDecode = raw;
                        break;
                    }
                }

                // Set TX and RX audio frequency immediately
                emit setTxAudioFreqSignal(freqHz);
                emit setRxAudioFreqSignal(freqHz);

                if (m_selectedDecode.isEmpty())
                    sendLine(QString("OK: selected %1 Hz (no decode here — valid for cq)")
                             .arg(freqHz));
                else
                    sendLine(QString("OK: selected %1")
                             .arg(formatDecodeForDisplay(m_selectedDecode)));
            }
        }
    }
    else if (cmd == "cq")
    {
        emit setTxAudioFreqSignal(m_selectedFreq);
        emit startCQSignal();   // MainWindow enables auto at next slot boundary
        sendLine(QString("OK: CQ queued at %1 Hz — will TX at next slot boundary").arg(m_selectedFreq));
    }
    else if (cmd == "answer")
    {
        if (m_selectedDecode.isEmpty())
        {
            sendLine(QString("ERR: no decode at %1 Hz — 'answer' requires a decoded station")
                     .arg(m_selectedFreq));
        }
        else
        {
            QStringList p = m_selectedDecode.trimmed().split(' ', Qt::SkipEmptyParts);
            if (p.size() < 5)
            {
                sendLine("ERR: cannot parse selected decode");
                sendPrompt();
                return;
            }

            QTime   t    = QTime::fromString(p[0], p[0].size() > 4 ? "hhmmss" : "hhmm");
            qint32  snr  = p[1].toInt();
            float   dt   = p[2].toFloat();
            quint32 freq = p[3].toUInt();
            QString mode = p[4];
            QString msg  = p.size() > 5 ? p.mid(5).join(' ') : QString{};

            emit replySignal(t, snr, dt, freq, mode, msg, false, 0);
            sendLine(QString("OK: answering %1")
                     .arg(formatDecodeForDisplay(m_selectedDecode)));
        }
    }
    else
    {
        sendLine(QString("ERR: unknown command '%1' — type 'help'").arg(cmd));
    }

    sendPrompt();
}

// ---------------------------------------------------------------------------
// "set" sub-commands
// ---------------------------------------------------------------------------

void TcpCliServer::handleSet(QStringList const& parts)
{
    // parts[0] == "set", parts[1] == key, parts[2..] == value
    QString key   = parts[1].toLower();
    QString value = parts.mid(2).join(' ');

    if (key == "callsign")
    {
        if (value.isEmpty())
        {
            sendLine("ERR: callsign value required");
        }
        else
        {
            emit setCallsignSignal(value.toUpper());
            sendLine(QString("OK: callsign = %1").arg(value.toUpper()));
        }
    }
    else if (key == "grid")
    {
        if (value.isEmpty())
        {
            sendLine("ERR: grid locator value required");
        }
        else
        {
            emit setGridSignal(value.toUpper());
            sendLine(QString("OK: grid = %1").arg(value.toUpper()));
        }
    }
    else if (key == "odd")
    {
        bool enable = !(value == "off" || value == "0" || value == "false");
        emit setTxFirstSignal(enable);
        sendLine(QString("OK: odd time slot TX %1").arg(enable ? "enabled" : "disabled"));
    }
    else if (key == "halt")
    {
        bool enable = (value == "off" || value == "0" || value == "false");
        emit setAutoSignal(enable);
        sendLine(QString("OK: TX %1").arg(enable ? "enabled" : "halted"));
    }
    else
    {
        sendLine(QString("ERR: unknown set key '%1'").arg(key));
    }
}

// ---------------------------------------------------------------------------
// Inbound spectrum + decode data from MainWindow
// ---------------------------------------------------------------------------

void TcpCliServer::onTxStart(QString message)
{
    if (m_state == State::Unauthed || !m_client) return;
    QString freqTag = (m_selectedFreq >= 0)
        ? QString(" %1Hz").arg(m_selectedFreq)
        : QString();
    QString slotTag = m_txFirst ? " Odd" : " Even";
    sendLine(QString("[%1%2%3] TX: %3").arg(freqTag).arg(slotTag).arg(message));
}

void TcpCliServer::onTxStop()
{
    if (m_state == State::Unauthed || !m_client) return;
    sendLine("TX STOP");
    sendPrompt();
}

void TcpCliServer::onTxFirstChanged(bool txFirst)
{
    m_txFirst = txFirst;
}

void TcpCliServer::onSpectrum(QVector<float> savg, float df3, int nfa, int nfb)
{
    m_pendingSpectrum  = savg;
    m_pendingDf3       = df3;
    m_pendingNfa       = nfa;
    m_pendingNfb       = nfb;
    m_spectrumPending  = true;
}

void TcpCliServer::onDecodes(QStringList decodes)
{
    if (!m_client || m_state == State::Unauthed) return;

    m_lastDecodes = decodes;

    // Refresh m_selectedDecode against the new batch (freq selection persists;
    // decode at that freq may appear or disappear each period).
    if (m_selectedFreq >= 0)
    {
        m_selectedDecode.clear();
        for (auto const& raw : m_lastDecodes)
        {
            if (extractFreqFromDecode(raw) == m_selectedFreq)
            {
                m_selectedDecode = raw;
                break;
            }
        }
    }

    // Emit spectrum line
    if (m_spectrumPending && !m_pendingSpectrum.isEmpty())
    {
        // CR overwrites '> ' prompt, then bar + marker
        m_client->write("\r\n");
        sendLine(renderSpectrum(m_pendingSpectrum, m_pendingDf3,
                                m_pendingNfa, m_pendingNfb,
                                m_selectedFreq, m_txFirst));
        m_spectrumPending = false;
    }

    // Emit decode listing — keyed by audio frequency
    for (auto const& raw : decodes)
        sendLine(formatDecodeForDisplay(raw));

    sendPrompt();
}

// ---------------------------------------------------------------------------
// Spectrum ASCII rendering
// ---------------------------------------------------------------------------

QString TcpCliServer::renderSpectrum(QVector<float> const& savg,
                                     float df3, int nfa, int nfb,
                                     int selectedHz, bool txFirst) const
{
    QString freqTag;
    Q_UNUSED(freqTag)

    // Build bar (spaces if no data)
    QString bar(m_spectrumWidth, ' ');

    if (!savg.isEmpty() && df3 > 0.f && nfb > nfa)
    {
        int lo = qMax(0,             static_cast<int>(nfa / df3));
        int hi = qMin(savg.size()-1, static_cast<int>(nfb / df3));
        if (hi <= lo) hi = qMin(savg.size()-1, lo + 1);
        int span = hi - lo;
        if (span < 1) span = 1;

        QVector<float> cols(m_spectrumWidth, -200.f);
        for (int bin = lo; bin <= hi; ++bin)
        {
            int col = static_cast<int>(
                static_cast<double>(bin - lo) / span * (m_spectrumWidth - 1));
            col = qBound(0, col, m_spectrumWidth - 1);
            cols[col] = qMax(cols[col], savg[bin]);
        }

        QVector<float> sorted = cols;
        std::sort(sorted.begin(), sorted.end());
        float noise = sorted[sorted.size() / 2];

        const QString levels {" .+\u2588"};
        bar.clear();
        bar.reserve(m_spectrumWidth);
        for (int c = 0; c < m_spectrumWidth; ++c)
        {
            float db = cols[c] - noise;
            if      (db <  3.f)  bar.append(levels[0]);
            else if (db < 10.f)  bar.append(levels[1]);
            else if (db < 20.f)  bar.append(levels[2]);
            else                 bar.append(levels[3]);
        }
    }

    QString ts = QDateTime::currentDateTimeUtc().toString("hh:mm:ss");
    QString barLine = QString("[%1] |%2|").arg(ts).arg(bar);

    // Marker line: '▲' aligned under selected freq, then "NHz, Slot"
    // prefix '[HH:MM:SS] |' is 12 chars
    QString markerLine;
    if (selectedHz >= 0 && nfb > nfa)
    {
        int col = static_cast<int>(
            static_cast<double>(selectedHz - nfa) / (nfb - nfa) * (m_spectrumWidth - 1));
        col = qBound(0, col, m_spectrumWidth - 1);
        markerLine = QString(12 + col, ' ')
            + QChar(0x25B2)  // ▲ UP-POINTING TRIANGLE
            + QString(" %1Hz, %2").arg(selectedHz).arg(txFirst ? "Odd" : "Even");
    }

    return markerLine.isEmpty() ? barLine : barLine + "\r\n" + markerLine;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

void TcpCliServer::sendLine(QString const& text)
{
    if (!m_client) return;
    m_client->write((text + "\r\n").toUtf8());
}

void TcpCliServer::sendPrompt()
{
    if (!m_client) return;
    m_client->write("> ");
    m_client->flush();
}

void TcpCliServer::printHelp()
{
    sendLine("Commands:");
    sendLine("  auth <password>       Authenticate (required if --cli-pass set)");
    sendLine("  set callsign <CALL>   Set my callsign");
    sendLine("  set grid <GRID>       Set my grid");
    sendLine("  set halt <on|off>     Halt/resume automatic TX");
    sendLine("  set odd <on|off>      Use odd time slot (TX first)");
    sendLine("  status                Show current state");

    sendLine("  select <Hz>           Select freq — sets TX+RX audio offset; optional decode target");

    sendLine("  cq                    Send CQ at selected freq (no decode needed)");
    sendLine("  answer                Reply to decode at selected freq (decode required)");
    sendLine("  help                  This help text");
    sendLine("  quit                  Close connection");
}

void TcpCliServer::printStatus()
{
    QString decode = m_selectedDecode.isEmpty()
        ? "(no decode)"
        : formatDecodeForDisplay(m_selectedDecode);
    sendLine(QString("selected: %1 Hz  %2  decodes this period: %3")
             .arg(m_selectedFreq)
             .arg(decode)
             .arg(m_lastDecodes.size()));
}

void TcpCliServer::printSelectedSlot()
{
    if (m_selectedFreq < 0)
    {
        sendLine("nothing selected");
        return;
    }
    if (m_selectedDecode.isEmpty())
    {
        sendLine(QString("selected %1 Hz (no decode — valid for cq)").arg(m_selectedFreq));
        return;
    }
    sendLine(QString("selected: %1").arg(formatDecodeForDisplay(m_selectedDecode)));
    QStringList p = m_selectedDecode.trimmed().split(' ', Qt::SkipEmptyParts);
    if (p.size() >= 5)
    {
        sendLine(QString("  time: %1  snr: %2 dB  dt: %3 s  freq: %4 Hz  mode: %5")
                 .arg(p[0]).arg(p[1]).arg(p[2]).arg(p[3]).arg(p[4]));
        if (p.size() > 5)
            sendLine(QString("  message: %1").arg(p.mid(5).join(' ')));
    }
}
