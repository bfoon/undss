import 'package:flutter/material.dart';
import 'package:intl/intl.dart';

import '../core/api.dart';
import '../core/theme.dart';
import '../widgets/common.dart';

class BookingScreen extends StatefulWidget {
  const BookingScreen({super.key});

  @override
  State<BookingScreen> createState() => _BookingScreenState();
}

class _BookingScreenState extends State<BookingScreen>
    with SingleTickerProviderStateMixin {
  late final TabController _tabs = TabController(length: 2, vsync: this);
  final GlobalKey<_MyBookingsTabState> _myBookings =
      GlobalKey<_MyBookingsTabState>();

  @override
  void dispose() {
    _tabs.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        Material(
          color: Colors.white,
          child: TabBar(
            controller: _tabs,
            labelColor: UnColors.darkBlue,
            indicatorColor: UnColors.blue,
            tabs: const [
              Tab(text: 'Rooms'),
              Tab(text: 'My bookings'),
            ],
          ),
        ),
        Expanded(
          child: TabBarView(
            controller: _tabs,
            children: [
              _RoomsTab(
                onBooked: () {
                  _myBookings.currentState?.refresh();
                  _tabs.animateTo(1);
                },
              ),
              _MyBookingsTab(key: _myBookings),
            ],
          ),
        ),
      ],
    );
  }
}

class _RoomsTab extends StatefulWidget {
  const _RoomsTab({required this.onBooked});

  final VoidCallback onBooked;

  @override
  State<_RoomsTab> createState() => _RoomsTabState();
}

class _RoomsTabState extends State<_RoomsTab> {
  late Future<List<dynamic>> _future = Api.instance.rooms();

  void refresh() => setState(() => _future = Api.instance.rooms());

  @override
  Widget build(BuildContext context) {
    return RefreshIndicator(
      onRefresh: () async => refresh(),
      child: FutureBuilder<List<dynamic>>(
        future: _future,
        builder: (context, snap) {
          if (snap.connectionState == ConnectionState.waiting) {
            return const Loading(message: 'Finding rooms…');
          }

          if (snap.hasError) {
            return ListView(
              children: [
                SizedBox(
                  height: 420,
                  child: FailureState(
                    message: '${snap.error}',
                    onRetry: refresh,
                  ),
                ),
              ],
            );
          }

          final rooms = snap.data ?? const [];
          if (rooms.isEmpty) {
            return ListView(
              children: const [
                SizedBox(
                  height: 420,
                  child: EmptyState(
                    icon: Icons.meeting_room_outlined,
                    title: 'No rooms available',
                    detail:
                        'Rooms shared with your office will appear here.',
                  ),
                ),
              ],
            );
          }

          return ListView.separated(
            padding: const EdgeInsets.all(UnStyle.gap),
            itemCount: rooms.length,
            separatorBuilder: (_, __) => const SizedBox(height: 10),
            itemBuilder: (context, index) {
              final room =
                  Map<String, dynamic>.from(rooms[index] as Map);
              final available = room['available_now'] == true;
              final capacity = room['capacity'];
              final amenities =
                  room['amenities'] as List<dynamic>? ?? const [];

              return InkWell(
                borderRadius: BorderRadius.circular(UnStyle.radius),
                onTap: () async {
                  final made = await Navigator.of(context).push<bool>(
                    MaterialPageRoute(
                      builder: (_) => BookRoomScreen(room: room),
                    ),
                  );
                  if (made == true) {
                    refresh();
                    widget.onBooked();
                  }
                },
                child: Container(
                  padding: const EdgeInsets.all(14),
                  decoration: UnStyle.card(),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Container(
                            height: 44,
                            width: 44,
                            decoration: BoxDecoration(
                              color: UnColors.lightBlue,
                              borderRadius: BorderRadius.circular(12),
                            ),
                            child: const Icon(
                              Icons.meeting_room_outlined,
                              color: UnColors.darkBlue,
                            ),
                          ),
                          const SizedBox(width: 12),
                          Expanded(
                            child: Column(
                              crossAxisAlignment:
                                  CrossAxisAlignment.start,
                              children: [
                                Text(
                                  '${room['name']}',
                                  style: const TextStyle(
                                    fontSize: 15.5,
                                    fontWeight: FontWeight.w700,
                                    color: UnColors.navy,
                                  ),
                                ),
                                if ('${room['location'] ?? ''}'
                                    .isNotEmpty)
                                  Text(
                                    '${room['location']}',
                                    style: const TextStyle(
                                      fontSize: 12.5,
                                      color: UnColors.muted,
                                    ),
                                  ),
                              ],
                            ),
                          ),
                          StatusPill(
                            available ? 'Free now' : 'In use',
                            color: available
                                ? UnColors.green
                                : UnColors.amber,
                          ),
                        ],
                      ),
                      const SizedBox(height: 10),
                      Wrap(
                        spacing: 8,
                        runSpacing: 6,
                        children: [
                          if (capacity != null)
                            StatusPill(
                              '$capacity people',
                              color: UnColors.blue,
                            ),
                          StatusPill(
                            '${room['type_label'] ?? room['type'] ?? 'Room'}',
                          ),
                          for (final raw in amenities.take(4))
                            if (raw is Map)
                              StatusPill('${raw['name']}'),
                        ],
                      ),
                      if ('${room['shared_note'] ?? ''}'.isNotEmpty) ...[
                        const SizedBox(height: 10),
                        Text(
                          '${room['shared_note']}',
                          style: const TextStyle(
                            fontSize: 12.5,
                            color: UnColors.muted,
                          ),
                        ),
                      ],
                    ],
                  ),
                ),
              );
            },
          );
        },
      ),
    );
  }
}

class _MyBookingsTab extends StatefulWidget {
  const _MyBookingsTab({super.key});

  @override
  State<_MyBookingsTab> createState() => _MyBookingsTabState();
}

class _MyBookingsTabState extends State<_MyBookingsTab> {
  late Future<List<dynamic>> _future = Api.instance.roomBookings();

  void refresh() =>
      setState(() => _future = Api.instance.roomBookings());

  Future<void> _cancel(Map<String, dynamic> booking) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: const Text('Cancel booking?'),
        content: Text(
          '${booking['title']}\n'
          '${booking['room_name']} · ${booking['date']} '
          '${booking['start_time']}–${booking['end_time']}',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, false),
            child: const Text('Keep booking'),
          ),
          FilledButton(
            style: FilledButton.styleFrom(
              backgroundColor: UnColors.red,
            ),
            onPressed: () => Navigator.pop(dialogContext, true),
            child: const Text('Cancel'),
          ),
        ],
      ),
    );

    if (confirmed != true) return;

    try {
      await Api.instance.cancelRoomBooking(booking['id'] as int);
      if (!mounted) return;
      showNote(context, 'Booking cancelled.');
      refresh();
    } on ApiException catch (e) {
      if (mounted) showNote(context, e.message, error: true);
    }
  }

  @override
  Widget build(BuildContext context) {
    return RefreshIndicator(
      onRefresh: () async => refresh(),
      child: FutureBuilder<List<dynamic>>(
        future: _future,
        builder: (context, snap) {
          if (snap.connectionState == ConnectionState.waiting) {
            return const Loading(message: 'Fetching your bookings…');
          }

          if (snap.hasError) {
            return ListView(
              children: [
                SizedBox(
                  height: 420,
                  child: FailureState(
                    message: '${snap.error}',
                    onRetry: refresh,
                  ),
                ),
              ],
            );
          }

          final bookings = snap.data ?? const [];
          if (bookings.isEmpty) {
            return ListView(
              children: const [
                SizedBox(
                  height: 420,
                  child: EmptyState(
                    icon: Icons.event_available_outlined,
                    title: 'No bookings yet',
                    detail:
                        'Bookings you create in the app or on the website appear here.',
                  ),
                ),
              ],
            );
          }

          return ListView.separated(
            padding: const EdgeInsets.all(UnStyle.gap),
            itemCount: bookings.length,
            separatorBuilder: (_, __) => const SizedBox(height: 10),
            itemBuilder: (context, index) {
              final booking = Map<String, dynamic>.from(
                bookings[index] as Map,
              );
              final status = '${booking['status']}';
              final canCancel =
                  status == 'pending' || status == 'approved';

              return Container(
                padding: const EdgeInsets.all(14),
                decoration: UnStyle.card(),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        Expanded(
                          child: Text(
                            '${booking['title']}',
                            style: const TextStyle(
                              fontWeight: FontWeight.w700,
                              color: UnColors.navy,
                            ),
                          ),
                        ),
                        StatusPill(
                          '${booking['status_label'] ?? status}',
                          color: _statusColor(status),
                        ),
                      ],
                    ),
                    const SizedBox(height: 5),
                    Text(
                      '${booking['room_name']} · ${booking['date']}',
                      style: const TextStyle(
                        color: UnColors.muted,
                        fontSize: 12.5,
                      ),
                    ),
                    Text(
                      '${booking['start_time']} – ${booking['end_time']}',
                      style: const TextStyle(
                        color: UnColors.muted,
                        fontSize: 12.5,
                      ),
                    ),
                    if ('${booking['rejection_reason'] ?? ''}'
                        .isNotEmpty) ...[
                      const SizedBox(height: 8),
                      Text(
                        'Reason: ${booking['rejection_reason']}',
                        style: const TextStyle(
                          color: UnColors.red,
                          fontSize: 12.5,
                        ),
                      ),
                    ],
                    if (canCancel) ...[
                      const SizedBox(height: 8),
                      Align(
                        alignment: Alignment.centerRight,
                        child: TextButton.icon(
                          onPressed: () => _cancel(booking),
                          icon: const Icon(
                            Icons.cancel_outlined,
                            size: 18,
                          ),
                          label: const Text('Cancel'),
                        ),
                      ),
                    ],
                  ],
                ),
              );
            },
          );
        },
      ),
    );
  }

  Color _statusColor(String status) {
    switch (status) {
      case 'approved':
        return UnColors.green;
      case 'pending':
        return UnColors.amber;
      case 'rejected':
      case 'cancelled':
        return UnColors.red;
      default:
        return UnColors.muted;
    }
  }
}

class BookRoomScreen extends StatefulWidget {
  const BookRoomScreen({super.key, required this.room});

  final Map<String, dynamic> room;

  @override
  State<BookRoomScreen> createState() => _BookRoomScreenState();
}

class _BookRoomScreenState extends State<BookRoomScreen> {
  final TextEditingController _title = TextEditingController();
  final TextEditingController _description = TextEditingController();
  final TextEditingController _attendees = TextEditingController();
  final TextEditingController _meetingLink = TextEditingController();

  DateTime _date = DateTime.now();
  TimeOfDay _start = const TimeOfDay(hour: 9, minute: 0);
  TimeOfDay _end = const TimeOfDay(hour: 10, minute: 0);
  String _ictSupport = 'none';
  bool _attendance = false;
  bool _inviteLink = false;
  bool _autoAccept = false;
  bool _busy = false;
  final Set<int> _amenities = <int>{};

  @override
  void dispose() {
    _title.dispose();
    _description.dispose();
    _attendees.dispose();
    _meetingLink.dispose();
    super.dispose();
  }

  String _time(TimeOfDay value) =>
      '${value.hour.toString().padLeft(2, '0')}:'
      '${value.minute.toString().padLeft(2, '0')}';

  Future<void> _pickDate() async {
    final chosen = await showDatePicker(
      context: context,
      initialDate: _date,
      firstDate: DateTime.now(),
      lastDate: DateTime.now().add(const Duration(days: 730)),
    );
    if (chosen != null) setState(() => _date = chosen);
  }

  Future<void> _pickTime(bool start) async {
    final chosen = await showTimePicker(
      context: context,
      initialTime: start ? _start : _end,
    );
    if (chosen == null) return;

    setState(() {
      if (start) {
        _start = chosen;
      } else {
        _end = chosen;
      }
    });
  }

  Future<void> _submit() async {
    if (_title.text.trim().isEmpty) {
      showNote(context, 'Enter a meeting title.', error: true);
      return;
    }

    final startMinutes = _start.hour * 60 + _start.minute;
    final endMinutes = _end.hour * 60 + _end.minute;
    if (endMinutes <= startMinutes) {
      showNote(
        context,
        'End time must be after the start time.',
        error: true,
      );
      return;
    }

    setState(() => _busy = true);

    try {
      final result = await Api.instance.bookRoom(
        widget.room['id'] as int,
        title: _title.text.trim(),
        description: _description.text.trim(),
        date: DateFormat('yyyy-MM-dd').format(_date),
        startTime: _time(_start),
        endTime: _time(_end),
        ictSupport: _ictSupport,
        attendeeEmails: _attendees.text.trim(),
        virtualMeetingLink: _meetingLink.text.trim(),
        amenityIds: _amenities.toList(),
        enableAttendance: _attendance,
        enableInviteLink: _inviteLink,
        autoAcceptRegistration: _autoAccept,
      );

      if (!mounted) return;
      showNote(context, '${result['message'] ?? 'Booking created.'}');
      Navigator.of(context).pop(true);
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() => _busy = false);
      showNote(context, e.message, error: true);
    }
  }

  @override
  Widget build(BuildContext context) {
    final amenities =
        widget.room['amenities'] as List<dynamic>? ?? const [];

    return Scaffold(
      appBar: AppBar(
        title: Text(
          'Book ${widget.room['name']}',
          overflow: TextOverflow.ellipsis,
        ),
      ),
      body: ListView(
        padding: const EdgeInsets.all(UnStyle.gap),
        children: [
          TextField(
            controller: _title,
            decoration: const InputDecoration(
              labelText: 'Meeting title *',
              border: OutlineInputBorder(),
            ),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _description,
            minLines: 2,
            maxLines: 4,
            decoration: const InputDecoration(
              labelText: 'Description',
              border: OutlineInputBorder(),
            ),
          ),
          const SizedBox(height: 12),
          ListTile(
            shape: RoundedRectangleBorder(
              side: const BorderSide(color: UnColors.line),
              borderRadius: BorderRadius.circular(10),
            ),
            leading: const Icon(Icons.calendar_today_outlined),
            title: const Text('Date'),
            subtitle: Text(DateFormat('EEE, d MMM yyyy').format(_date)),
            trailing: const Icon(Icons.chevron_right),
            onTap: _pickDate,
          ),
          const SizedBox(height: 10),
          Row(
            children: [
              Expanded(
                child: _timeTile('Start', _start, true),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: _timeTile('End', _end, false),
              ),
            ],
          ),
          if (amenities.isNotEmpty) ...[
            const SizedBox(height: 16),
            const Text(
              'Amenities',
              style: TextStyle(
                fontWeight: FontWeight.w700,
                color: UnColors.navy,
              ),
            ),
            const SizedBox(height: 8),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                for (final raw in amenities)
                  if (raw is Map)
                    FilterChip(
                      label: Text('${raw['name']}'),
                      selected: _amenities.contains(raw['id'] as int),
                      onSelected: (selected) {
                        setState(() {
                          final id = raw['id'] as int;
                          if (selected) {
                            _amenities.add(id);
                          } else {
                            _amenities.remove(id);
                          }
                        });
                      },
                    ),
              ],
            ),
          ],
          const SizedBox(height: 16),
          DropdownButtonFormField<String>(
            value: _ictSupport,
            decoration: const InputDecoration(
              labelText: 'ICT support',
              border: OutlineInputBorder(),
            ),
            items: const [
              DropdownMenuItem(
                value: 'none',
                child: Text('No ICT support needed'),
              ),
              DropdownMenuItem(
                value: 'setup',
                child: Text('Setup / AV before meeting'),
              ),
              DropdownMenuItem(
                value: 'during',
                child: Text('Live support during meeting'),
              ),
            ],
            onChanged: (value) =>
                setState(() => _ictSupport = value ?? 'none'),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _attendees,
            maxLines: 2,
            decoration: const InputDecoration(
              labelText: 'Attendee emails',
              hintText: 'name@example.org, another@example.org',
              border: OutlineInputBorder(),
            ),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _meetingLink,
            keyboardType: TextInputType.url,
            decoration: const InputDecoration(
              labelText: 'Teams / Zoom / virtual meeting link',
              border: OutlineInputBorder(),
            ),
          ),
          const SizedBox(height: 8),
          SwitchListTile(
            value: _attendance,
            onChanged: (value) => setState(() => _attendance = value),
            title: const Text('Enable digital attendance'),
          ),
          SwitchListTile(
            value: _inviteLink,
            onChanged: (value) => setState(() {
              _inviteLink = value;
              if (!value) _autoAccept = false;
            }),
            title: const Text('Create public invite/registration link'),
          ),
          if (_inviteLink)
            SwitchListTile(
              value: _autoAccept,
              onChanged: (value) =>
                  setState(() => _autoAccept = value),
              title: const Text('Auto-accept registrations'),
            ),
          const SizedBox(height: 16),
          if (_busy)
            const Center(child: CircularProgressIndicator())
          else
            FilledButton.icon(
              onPressed: _submit,
              icon: const Icon(Icons.event_available),
              label: const Text('Book room'),
            ),
        ],
      ),
    );
  }

  Widget _timeTile(
    String label,
    TimeOfDay value,
    bool start,
  ) {
    return InkWell(
      onTap: () => _pickTime(start),
      borderRadius: BorderRadius.circular(10),
      child: InputDecorator(
        decoration: InputDecoration(
          labelText: label,
          border: const OutlineInputBorder(),
          suffixIcon: const Icon(Icons.schedule),
        ),
        child: Text(value.format(context)),
      ),
    );
  }
}
